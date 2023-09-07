"""Test the HyperGat class."""

import numpy as np
import torch

from topomodelx.nn.hypergraph.hypergat import HyperGAT


class TestHNHN:
    """Test the HyperGAT."""

    def test_fowared(self):
        """Test forward method."""
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        incidence = torch.from_numpy(np.random.rand(2, 2)).to_sparse()
        incidence = incidence.float().to(device)
        model = HyperGAT(in_channels=2, out_channels=2, n_layers=1).to(device)

        x_0 = torch.rand(2, 2)

        x_0 = torch.tensor(x_0).float().to(device)

        y1 = model(x_0, incidence)

        assert len(y1) != 0
