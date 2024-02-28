"""
macepotential.py: Implements the MACE potential function.

This is part of the OpenMM molecular simulation toolkit originating from
Simbios, the NIH National Center for Physics-Based Simulation of
Biological Structures at Stanford, funded under the NIH Roadmap for
Medical Research, grant U54 GM072970. See https://simtk.org.

Portions copyright (c) 2021 Stanford University and the Authors.
Authors: Peter Eastman
Contributors: Stephen Farr, Joao Morado

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
THE AUTHORS, CONTRIBUTORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
USE OR OTHER DEALINGS IN THE SOFTWARE.
"""
import openmm
from openmmml.mlpotential import MLPotential, MLPotentialImpl, MLPotentialImplFactory
from typing import Iterable, Optional, Tuple


class MACEPotentialImplFactory(MLPotentialImplFactory):
    """This is the factory that creates MACEPotentialImpl objects."""

    def createImpl(
        self, name: str, modelPath: Optional[str] = None, **args
    ) -> MLPotentialImpl:
        return MACEPotentialImpl(name, modelPath)


class MACEPotentialImpl(MLPotentialImpl):
    """This is the MLPotentialImpl implementing the MACE potential.

    The MACE potential is constructed using MACE to build a PyTorch model,
    and then integrated into the OpenMM System using a TorchForce.
    This implementation supports both MACE-OFF23 and locally trained MACE models.

    To use one of the pre-trained MACE-OFF23 models, specify the model name. For example:
    >>> potential = MLPotential('mace-off23-small')

    Other available MACE-OFF23 models include `mace-off23-medium` and `mace-off23-large`.

    To use a locally trained MACE model, provide the path to the model file. For example:
    >>> potential = MLPotential('mace', modelPath='MACE.model')

    During system creation, you can optionally specify the precision of the model using the
    `precision` keyword argument. Supported options are 'single' and 'double'. For example:
    >>> system = potential.createSystem(topology, precision='single')

    By default, the implementation uses the precision of the loaded MACE model.
    According to the MACE documentation, 'single' precision is recommended for MD (faster but
    less accurate), while 'double' precision is recommended for geometry optimization.

    Additionally, you can request computation of the full atomic energy, including the atom
    self-energy, instead of the default interaction energy, by setting `returnEnergyType` to
    `energy`. For example:
    >>> system = potential.createSystem(topology, returnEnergyType=`energy`)

    The default is to compute the interaction energy, which can be made explicit by setting
    `returnEnergyType=interaction_energy`.

    Attributes
    ----------
    name : str
        The name of the MACE model.
    modelPath : str
        The path to the locally trained MACE model if `name` is `mace`.
    """

    def __init__(self, name: str, modelPath) -> None:
        """
        Initialize the MACEPotentialImpl.

        Parameters
        ----------
        name : str
            The name of the MACE model.
            Options include `mace-off23-small`, `mace-off23-medium`, `mace-off23-large`, and `mace`.
        modelPath : str, optional
            The path to the locally trained MACE model if `name` is `mace`.
        """
        self.name = name
        self.modelPath = modelPath

    def addForces(
        self,
        topology: openmm.app.Topology,
        system: openmm.System,
        atoms: Optional[Iterable[int]],
        forceGroup: int,
        precision: Optional[str] = None,
        returnEnergyType: str = "energy",
        filename: str = "macemodel.pt",
        **args,
    ) -> None:
        """
        Add the MACEForce to the OpenMM System.

        Parameters
        ----------
        topology : openmm.app.Topology
            The topology of the system.
        system : openmm.System
            The system to which the force will be added.
        atoms : iterable of int
            The indices of the atoms to include in the model. If None, all atoms are included.
        forceGroup : int
            The force group to which the force should be assigned.
        precision : str, optional
            The precision of the model. Supported options are 'single' and 'double'.
            If None, the default precision of the model is used.
        returnEnergyType : str, optional
            Whether to return the interaction energy or the energy including the self-energy.
            Default is `interaction_energy`. Supported options are `interaction_energy` and `energy`.
        filename : str, optional
            The filename to which the TorchForce will be saved. Default is 'macemodel.pt'.
        """
        import torch
        import openmmtorch

        try:
            from mace.tools import utils, to_one_hot, atomic_numbers_to_indices
            from mace.calculators.foundations_models import mace_off
        except ImportError as e:
            raise ImportError(
                f"Failed to import `mace` with error: {e}. Install `mace` with `pip install torch-mace`."
            )
        try:
            from e3nn.util import jit
        except ImportError as e:
            raise ImportError(
                f"Failed to import `e3nn` with error: {e}. Install `e3nn` with `pip install e3nn`."
            )
        try:
            from NNPOps.neighbors import getNeighborPairs
        except ImportError as e:
            raise ImportError(
                f"Failed to import `NNPOps` with error: {e}."
                "Install `nnpops` with `conda install -c conda-forge nnpops`."
            )

        assert (
            returnEnergyType
            in [
                "interaction_energy",
                "energy",
            ]
        ), "Supported options for `returnEnergyType` are `interaction_energy` and `energy`."

        # Load the model and compile it to TorchScript.
        if self.name.startswith("mace-off23"):
            size = self.name.split("-")[-1]
            assert (
                size in ["small", "medium", "large"]
            ), "Available MACE-OFF23 models are: `mace-off23-small`, `mace-off23-medium`, `mace-off23-large`"
            model = mace_off(model=size, return_raw_model=True)
        elif self.name == "mace":
            if self.modelPath is not None:
                model = torch.load(self.modelPath)
            else:
                raise ValueError("No `modelPath` provided for local MACE model.")
        else:
            raise ValueError(f"Unsupported MACE model: {self.name}")
        model = jit.compile(model)

        # Get the atomic numbers of the ML region.
        includedAtoms = list(topology.atoms())
        if atoms is not None:
            includedAtoms = [includedAtoms[i] for i in atoms]
        atomicNumbers = [atom.element.atomic_number for atom in includedAtoms]

        # Set the precision that the model will be used with.
        modelDefaultDtype = next(model.parameters()).dtype
        if precision is None:
            dtype = modelDefaultDtype
        elif precision == "single":
            dtype = torch.float32
        elif precision == "double":
            dtype = torch.float64
        else:
            raise ValueError(
                f"Unsupported precision `{precision}` for the model. "
                "Supported values are `single` and `double`."
            )
        if dtype != modelDefaultDtype:
            print(
                f"Model dtype is {modelDefaultDtype} "
                f"and requested dtype is {dtype}. "
                "The model will be converted to the requested dtype."
            )

        # One hot encoding of atomic numbers
        zTable = utils.AtomicNumberTable([int(z) for z in model.atomic_numbers])
        nodeAttrs = to_one_hot(
            torch.tensor(
                atomic_numbers_to_indices(atomicNumbers, z_table=zTable),
                dtype=torch.long,
            ).unsqueeze(-1),
            num_classes=len(zTable),
        )

        class MACEForce(torch.nn.Module):
            """
            MACEForce class to be used with TorchForce.

            Parameters
            ----------
            model : torch.jit._script.RecursiveScriptModule
                The compiled MACE model.
            dtype : torch.dtype
                The precision with which the model will be used.
            energyScale : float
                Conversion factor for the energy, viz. eV to kJ/mol.
            lengthScale : float
                Conversion factor for the length, viz. nm to Angstrom.
            indices : torch.Tensor
                The indices of the atoms to calculate the energy for.
            returnEnergyType : str
                Whether to return the interaction energy or the energy including the self-energy.
            inputDict : dict
                The input dictionary passed to the model.
            """

            def __init__(
                self,
                model: torch.jit._script.RecursiveScriptModule,
                nodeAttrs: torch.Tensor,
                atoms: Optional[Iterable[int]],
                periodic: bool,
                dtype: torch.dtype,
                returnEnergyType: str,
            ) -> None:
                """
                Initialize the MACEForce.

                Parameters
                ----------
                model : torch.jit._script.RecursiveScriptModule
                    The MACE model.
                nodeAttrs : torch.Tensor
                    The one-hot encoded atomic numbers.
                atoms : iterable of int
                    The indices of the atoms. If None, all atoms are included.
                periodic : bool
                    Whether the system is periodic.
                dtype : torch.dtype
                    The precision of the model.
                returnEnergyType : str
                    Whether to return the interaction energy or the energy including the self-energy.
                """
                super(MACEForce, self).__init__()

                self.dtype = dtype
                self.model = model.to(self.dtype)
                self.energyScale = 96.4853
                self.lengthScale = 10.0
                self.returnEnergyType = returnEnergyType

                if atoms is None:
                    self.indices = None
                else:
                    self.indices = torch.tensor(sorted(atoms), dtype=torch.int64)

                # Create the default input dict.
                self.inputDict = {
                    "ptr": torch.tensor(
                        [0, nodeAttrs.shape[0]], dtype=torch.long, requires_grad=False
                    ),
                    "node_attrs": nodeAttrs.to(self.dtype),
                    "batch": torch.zeros(
                        nodeAttrs.shape[0], dtype=torch.long, requires_grad=False
                    ),
                    "pbc": torch.tensor(
                        [periodic, periodic, periodic],
                        dtype=torch.bool,
                        requires_grad=False,
                    ),
                }

            def _getNeighborPairs(
                self, positions: torch.Tensor, cell: Optional[torch.Tensor]
            ) -> Tuple[torch.Tensor, torch.Tensor]:
                """
                Get the shifts and edge indices.

                This method calculates the shifts and edge indices by determining neighbor pairs (`neighbors`)
                and respective wrapped distances (`wrappedDeltas`) using `NNPOps.neighbors.getNeighborPairs`.
                After obtaining the `neighbors` and `wrappedDeltas`, the pairs with negative indices (r>cutoff)
                are filtered out, and the edge indices and shifts are finally calculated.

                Parameters
                ----------
                positions : torch.Tensor
                    The positions of the atoms.
                cell : torch.Tensor
                    The cell vectors.

                Returns
                -------
                edgeIndex : torch.Tensor
                    The edge indices.
                shifts : torch.Tensor
                    The shifts.
                """
                # Get the neighbor pairs, shifts and edge indeces.
                neighbors, wrappedDeltas, _, _ = getNeighborPairs(
                    positions, self.model.r_max, -1, cell
                )
                mask = neighbors >= 0
                neighbors = neighbors[mask].view(2, -1)
                wrappedDeltas = wrappedDeltas[mask[0], :]

                edgeIndex = torch.hstack((neighbors, neighbors.flip(0))).to(torch.int64)
                if cell is not None:
                    deltas = positions[edgeIndex[0]] - positions[edgeIndex[1]]
                    wrappedDeltas = torch.vstack((wrappedDeltas, -wrappedDeltas))
                    shiftsIdx = torch.mm(deltas - wrappedDeltas, torch.linalg.inv(cell))
                    shifts = torch.mm(shiftsIdx, cell)
                else:
                    shifts = torch.zeros((edgeIndex.shape[1], 3), dtype=self.dtype)

                return edgeIndex, shifts

            def forward(
                self, positions: torch.Tensor, boxvectors: Optional[torch.Tensor] = None
            ) -> torch.Tensor:
                """
                Forward pass of the model.

                Parameters
                ----------
                positions : torch.Tensor
                    The positions of the atoms.
                box_vectors : torch.Tensor
                    The box vectors.

                Returns
                -------
                energy : torch.Tensor
                    The predicted energy in kJ/mol.
                """
                # Setup positions and cell.
                if self.indices is not None:
                    positions = positions[self.indices]

                positions = positions.to(self.dtype) * self.lengthScale

                if boxvectors is not None:
                    cell = boxvectors.to(self.dtype) * self.lengthScale
                else:
                    cell = None

                # Get the shifts and edge indices.
                edgeIndex, shifts = self._getNeighborPairs(positions, cell)

                # Update input dictionary.
                self.inputDict["positions"] = positions
                self.inputDict["edge_index"] = edgeIndex
                self.inputDict["shifts"] = shifts

                # Predict the energy.
                energy = self.model(self.inputDict, compute_force=False)[
                    self.returnEnergyType
                ]

                assert (
                    energy is not None
                ), "The model did not return any energy. Please check the input."

                return energy * self.energyScale

        isPeriodic = (
            topology.getPeriodicBoxVectors() is not None
        ) or system.usesPeriodicBoundaryConditions()

        maceForce = MACEForce(
            model,
            nodeAttrs,
            atoms,
            isPeriodic,
            dtype,
            returnEnergyType,
        )

        # Convert it to TorchScript and save it.
        module = torch.jit.script(maceForce)
        module.save(filename)

        # Create the TorchForce and add it to the System.
        force = openmmtorch.TorchForce(filename)
        force.setForceGroup(forceGroup)
        force.setUsesPeriodicBoundaryConditions(isPeriodic)
        system.addForce(force)


MLPotential.registerImplFactory("mace", MACEPotentialImplFactory())
MLPotential.registerImplFactory("mace-off23-small", MACEPotentialImplFactory())
MLPotential.registerImplFactory("mace-off23-medium", MACEPotentialImplFactory())
MLPotential.registerImplFactory("mace-off23-large", MACEPotentialImplFactory())
