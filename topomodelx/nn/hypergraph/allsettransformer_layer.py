"""AllSetTransformer Layer Module."""
import torch
import torch.nn.functional as F
from torch import nn
from torch.nn.parameter import Parameter
from torch_geometric.utils import softmax

from topomodelx.base.message_passing import MessagePassing


class AllSetTransformerLayer(nn.Module):
    """AllSet Layer Module.

    A module for AllSet layer in a bipartite graph.

    Parameters
    ----------
    in_dim : int
        Dimension of the input features.
    hid_dim : int
        Dimension of the hidden features.
    out_dim : int
        Dimension of the output features.
    dropout : float
        Dropout probability.
    input_dropout : float, optional
        Dropout probability for the layer input. Defaults to 0.2.
    mlp_num_layers : int, optional
        Number of layers in the MLP. Defaults to 2.
    mlp_input_norm : bool, optional
        Whether to apply input normalization in the MLP. Defaults to False.
    heads : int or None, optional
        Number of attention heads. If None, attention is disabled. Defaults to None.
    PMA : bool, optional
        Whether to use the PMA (Prototype Matrix Attention) mechanism. Defaults to False.
    """

    def __init__(
        self,
        in_channels,
        hid_channels,
        out_channels,
        dropout=0.2,
        heads=None,
        att_dropout=0.0,
    ):
        super().__init__()  # AllSetLayer, self

        assert heads is not None, "AllSetTransformer requires heads to be specified."
        self.dropout = dropout

        self.v2e = AllSetTransformerConv(
            in_channels=in_channels,
            hid_channels=hid_channels,
            out_channels=hid_channels,
            att_dropout=att_dropout,
            att=True,
            heads=heads,
        )

        self.e2v = AllSetTransformerConv(
            in_channels=hid_channels,
            hid_channels=hid_channels,
            out_channels=out_channels,
            att_dropout=att_dropout,
            att=True,
            heads=heads,
        )

    def forward(self, x, incidence_1):
        """
        Forward computation.

        Parameters
        ----------
        x : torch.Tensor
            Input features.
        edge_index : torch.Tensor
            Edge list (of size (2, |E|)) where edge_index[0] contains nodes and edge_index[1] contains hyperedges.
        reversed_edge_index : torch.Tensor
            Edge list (of size (2, |E|)) where reversed_edge_index[0] contains hyperedges and reversed_edge_index[1] contains nodes.

        Returns
        -------
        x : torch.Tensor
            Output features.
        """
        if x.shape[-2] != incidence_1.shape[-2]:
            raise ValueError(
                f"Shape of input node features {x.shape[-2]} does not have the correct number of edges {incidence_1.shape[-2]}."
            )

        x = F.dropout(x, p=self.input_dropout, training=self.training)

        x = F.relu(self.v2e(x, incidence_1.transpose(1, 0)))
        x = F.dropout(x, p=self.dropout, training=self.training)

        x = F.relu(self.e2v(x, incidence_1))
        x = F.dropout(x, p=self.dropout, training=self.training)

        return x


class AllSetTransformerConv(MessagePassing):
    """Message passing: steps 1, 2, and 3.

    Builds the message passing route given by one neighborhood matrix.
    Includes an option for a x-specific update function.

    Parameters
    ----------
    in_channels : int
        Dimension of input features.
    out_channels : int
        Dimension of output features.
    aggr_norm : bool
        Whether to normalize the aggregated message by the neighborhood size.
    update_func : string
        Update method to apply to message.
    att : bool
        Whether to use attention.
        Optional, default: False.
    initialization : string
        Initialization method.
    """

    def __init__(
        self,
        in_channels,
        hid_channels,
        out_channels,
        aggr_norm=False,
        update_func=None,
        # Transformer parameters
        heads=8,
        att_dropout=0.0,
        negative_slope=0.2,
        # Attention
        att=True,
        initialization="xavier_uniform",
    ):
        super().__init__(
            att=att,
            initialization=initialization,
        )

        # assert att == True, "AllSetTransformerConv only works with attention"
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.aggr_norm = aggr_norm
        self.update_func = update_func

        self.hidden = hid_channels // heads
        self.out_channels = out_channels
        self.heads = heads

        self.negative_slope = negative_slope
        self.dropout = att_dropout

        # For neighbor nodes (source side, key)
        self.lin_K = torch.nn.Linear(in_channels, self.heads * self.hidden)

        # For neighbor nodes (source side, value)
        self.lin_V = torch.nn.Linear(in_channels, self.heads * self.hidden)

        # Seed vector
        self.att_weight = torch.nn.Parameter(torch.Tensor(1, self.heads, self.hidden))

        self.weight = Parameter(torch.Tensor(self.in_channels, self.out_channels))

        self.rFF = MLP(
            in_dim=self.heads * self.hidden,
            hid_dim=self.heads * self.hidden,
            out_dim=out_channels,
            num_layers=2,
            dropout=0.0,
        )
        # originally the normalisation should be NONE!!!!!!!!!!!!
        # Normalization='None',)!!!!!!!!!!

        self.ln0 = nn.LayerNorm(self.heads * self.hidden)
        self.ln1 = nn.LayerNorm(self.heads * self.hidden)

        self.reset_parameters()

    def update(self, x_message_on_target, x_target=None):
        """Update embeddings on each cell (step 4).

        Parameters
        ----------
        x_message_on_target : torch.Tensor, shape=[n_target_cells, out_channels]
            Output features on target cells.

        Returns
        -------
        _ : torch.Tensor, shape=[n_target_cells, out_channels]
            Updated output features on target cells.
        """
        if self.update_func == "sigmoid":
            return torch.sigmoid(x_message_on_target)
        if self.update_func == "relu":
            return torch.nn.functional.relu(x_message_on_target)

    def attention(self, x_source, alpha):
        """Compute attention weights for messages.

        This provides a default attention function to the message passing scheme.

        Alternatively, users can subclass MessagePassing and overwrite
        the attention method in order to replace it with their own attention mechanism.

        Details in [H23]_, Definition of "Attention Higher-Order Message Passing".

        Parameters
        ----------
        x_source : torch.Tensor, shape=[n_source_cells, in_channels]
            Input features on source cells.
            Assumes that all source cells have the same rank r.
        x_target : torch.Tensor, shape=[n_target_cells, in_channels]
            Input features on source cells.
            Assumes that all source cells have the same rank r.

        Returns
        -------
        _ : torch.Tensor, shape = [n_messages, 1]
            Attention weights: one scalar per message between a source and a target cell.
        """
        x_K = self.lin_K(x_source).view(-1, self.heads, self.hidden)
        x_V = self.lin_V(x_source).view(-1, self.heads, self.hidden)

        # Pointwise product X_k * a (weights every feature and sum across features)
        # output size: (|SET| x num_heads)
        alpha = (x_K * self.att_weight).sum(dim=-1)

        # Normalize with softmax over source nodes

        alpha = F.leaky_relu(alpha, self.negative_slope)
        alpha = softmax(
            alpha[self.source_index_j],
            self.source_index_j,
            None,
            self.source_index_j.max() + 1,
        )
        # self._alpha = alpha
        alpha = F.dropout(alpha, p=self.dropout, training=self.training)
        x_message = x_V[self.source_index_j] * alpha.unsqueeze(-1)

        return self.aggregate(x_message)

    def forward(self, x_source, neighborhood, x_target=None):
        """Forward pass.

        This implements message passing:
        - from source cells with input features `x_source`,
        - via `neighborhood` defining where messages can pass,
        - to target cells with input features `x_target`.

        In practice, this will update the features on the target cells.

        If not provided, x_target is assumed to be x_source,
        i.e. source cells send messages to themselves.

        Parameters
        ----------
        x_source : Tensor, shape=[..., n_source_cells, in_channels]
            Input features on source cells.
            Assumes that all source cells have the same rank r.
        neighborhood : torch.sparse, shape=[n_target_cells, n_source_cells]
            Neighborhood matrix.
        x_target : Tensor, shape=[..., n_target_cells, in_channels]
            Input features on target cells.
            Assumes that all target cells have the same rank s.
            Optional. If not provided, x_target is assumed to be x_source,
            i.e. source cells send messages to themselves.

        Returns
        -------
        _ : Tensor, shape=[..., n_target_cells, out_channels]
            Output features on target cells.
            Assumes that all target cells have the same rank s.
        """
        neighborhood = neighborhood.coalesce()
        self.target_index_i, self.source_index_j = neighborhood.indices()
        x_message_on_target = self.attention(x_source, x_target)

        # Skip-connection
        x_message_on_target = x_message_on_target + self.att_weight

        x_message_on_target = self.ln0(
            x_message_on_target.view(-1, self.heads * self.hidden)
        )
        # rFF and skip connection. Lhs of eq(7) in GMT paper.
        x_message_on_target = self.ln1(
            x_message_on_target + F.relu(self.rFF(x_message_on_target))
        )

        if self.update_func is None:
            return x_message_on_target

        return self.update(x_message_on_target, x_target)


class MLP(nn.Module):
    """MLP Module.

    A multi-layer perceptron module with optional normalization.

    Parameters
    ----------
    in_dim : int
        Dimension of the input features.
    hid_dim : int
        Dimension of the hidden features.
    out_dim : int
        Dimension of the output features.
    num_layers : int
        Number of layers in the MLP.
    dropout : float, optional
        Dropout probability. Defaults to 0.5.
    input_norm : bool, optional
        Whether to apply input normalization. Defaults to False.
    """

    def __init__(
        self, in_dim, hid_dim, out_dim, num_layers, dropout=0.5, layer_norm="None"
    ):
        super(MLP, self).__init__()
        self.lins = nn.ModuleList()
        self.normalizations = nn.ModuleList()
        assert layer_norm in ["None", "ln", "bn"]

        if layer_norm == "ln":
            layer_norm_func = nn.LayerNorm
        elif layer_norm == "bn":
            layer_norm_func = nn.BatchNorm1d
        else:
            layer_norm_func = nn.Identity

        if num_layers == 1:
            # Just a linear layer i.e. logistic regression
            self.normalizations.append(layer_norm_func(in_dim))
            self.lins.append(nn.Linear(in_dim, out_dim))
        else:
            self.normalizations.append(layer_norm_func(in_dim))
            self.lins.append(nn.Linear(in_dim, hid_dim))
            self.normalizations.append(layer_norm_func(hid_dim))
            for _ in range(num_layers - 2):
                self.lins.append(nn.Linear(hid_dim, hid_dim))
                self.normalizations.append(nn.LayerNorm(hid_dim))
            self.lins.append(nn.Linear(hid_dim, out_dim))

        self.dropout = dropout

    def reset_parameters(self):
        """Reset learnable parameters."""
        for lin in self.lins:
            lin.reset_parameters()
        for normalization in self.normalizations:
            if not (normalization.__class__.__name__ != "Identity"):
                normalization.reset_parameters()

    def forward(self, x):
        """
        Forward computation.

        Parameters
        ----------
        x : torch.Tensor
            Input features.

        Returns
        -------
        x : torch.Tensor
            Output features.
        """
        x = self.normalizations[0](x)
        for i, lin in enumerate(self.lins[:-1]):
            x = lin(x)
            x = F.relu(x, inplace=True)
            x = self.normalizations[i + 1](x)
            x = F.dropout(x, p=self.dropout, training=self.training)
        x = self.lins[-1](x)
        return x
