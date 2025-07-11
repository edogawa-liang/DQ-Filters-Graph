import torch
import torch.nn.functional as F
from torch_geometric.nn import GCNConv

class GCN2Classifier(torch.nn.Module):
    def __init__(self, in_channels, hidden_channels=64, out_channels=2):
        """
        Initializes a 2-layer GCN model.
        """
        super().__init__()
        self.conv1 = GCNConv(in_channels, hidden_channels, cached=False, normalize=True)
        self.conv2 = GCNConv(hidden_channels, out_channels, cached=False, normalize=True)

    def forward(self, x, edge_index, edge_weight=None):
        x = self.conv1(x, edge_index, edge_weight).relu()
        x = F.dropout(x, p=0.3, training=self.training)
        x = self.conv2(x, edge_index, edge_weight)
        return x



# 還沒檢查
class GCN2ClassifierWithGate(torch.nn.Module):
    def __init__(self, in_channels, hidden_channels=64, out_channels=2):
        """
        A 2-layer GCN model with separate message passing for node-node and node-feature edges,
        using a learnable gate to combine them.
        """
        super().__init__()
        self.conv1_node = GCNConv(in_channels, hidden_channels, cached=False, normalize=True)
        self.conv1_feat = GCNConv(in_channels, hidden_channels, cached=False, normalize=True)

        self.conv2_node = GCNConv(hidden_channels, out_channels, cached=False, normalize=True)
        self.conv2_feat = GCNConv(hidden_channels, out_channels, cached=False, normalize=True)

        self.gate1 = torch.nn.Linear(in_channels, 1)
        self.gate2 = torch.nn.Linear(hidden_channels, 1)

    def forward(self, x, edge_index, edge_weight, node_node_mask, node_feat_mask, is_original_node):
        # === Split edges ===
        edge_index_node = edge_index[:, node_node_mask]
        edge_index_feat = edge_index[:, node_feat_mask]
        edge_weight_node = edge_weight[node_node_mask]
        edge_weight_feat = edge_weight[node_feat_mask]

        # === Layer 1 ===
        h_node1 = self.conv1_node(x, edge_index_node, edge_weight_node)
        h_feat1 = self.conv1_feat(x, edge_index_feat, edge_weight_feat)

        gate1_raw = torch.sigmoid(self.gate1(x))  # shape [N+F, 1]
        gate1 = gate1_raw[is_original_node]  # 只保留原始節點的 gate

        h_mix1 = torch.zeros_like(h_node1)
        h_mix1[is_original_node] = gate1 * h_node1[is_original_node] + (1 - gate1) * h_feat1[is_original_node]
        h_mix1 = h_mix1.relu()
        h_mix1 = F.dropout(h_mix1, p=0.3, training=self.training)

        # === Layer 2 ===
        h_node2 = self.conv2_node(h_mix1, edge_index_node, edge_weight_node)
        h_feat2 = self.conv2_feat(h_mix1, edge_index_feat, edge_weight_feat)

        gate2_raw = torch.sigmoid(self.gate2(h_mix1))
        gate2 = gate2_raw[is_original_node]

        h_mix2 = torch.zeros_like(h_node2)
        h_mix2[is_original_node] = gate2 * h_node2[is_original_node] + (1 - gate2) * h_feat2[is_original_node]

        return h_mix2



class GCN2Regressor(torch.nn.Module):
    def __init__(self, in_channels, hidden_channels=64):
        """
        2-layer GCN model for node regression.
        """
        super().__init__()
        self.conv1 = GCNConv(in_channels, hidden_channels, cached=True, normalize=True)
        self.conv2 = GCNConv(hidden_channels, 1, cached=True, normalize=True)  # 回歸輸出 1 個數值

    def forward(self, x, edge_index, edge_weight=None):
        x = self.conv1(x, edge_index, edge_weight).relu()
        x = F.dropout(x, p=0.3, training=self.training)
        x = self.conv2(x, edge_index, edge_weight)  # 直接輸出回歸值
        return x.view(-1)  # 展平成一維



class GCN3Classifier(torch.nn.Module):
    def __init__(self, in_channels, hidden_channels1=64, hidden_channels2=32, out_channels=4):
        """
        Initializes a 3-layer GCN model.
        """
        super().__init__()
        self.conv1 = GCNConv(in_channels, hidden_channels1, cached=True, normalize=True)
        self.conv2 = GCNConv(hidden_channels1, hidden_channels2, cached=True, normalize=True)
        self.conv3 = GCNConv(hidden_channels2, out_channels, cached=True, normalize=True)

    def forward(self, x, edge_index, edge_weight=None):
        x = self.conv1(x, edge_index, edge_weight).relu()
        x = F.dropout(x, p=0.2, training=self.training)
        x = self.conv2(x, edge_index, edge_weight).relu()
        x = F.dropout(x, p=0.4, training=self.training)
        x = self.conv3(x, edge_index, edge_weight)
        return x


class GCN3Regressor(torch.nn.Module):
    def __init__(self, in_channels, hidden_channels1=64, hidden_channels2=32):
        """
        3-layer GCN model for node regression.
        """
        super().__init__()
        self.conv1 = GCNConv(in_channels, hidden_channels1, cached=True, normalize=True)
        self.conv2 = GCNConv(hidden_channels1, hidden_channels2, cached=True, normalize=True)
        self.conv3 = GCNConv(hidden_channels2, 1, cached=True, normalize=True)  # 回歸輸出 1 個數值

    def forward(self, x, edge_index, edge_weight=None):
        x = self.conv1(x, edge_index, edge_weight).relu()
        x = F.dropout(x, p=0.2, training=self.training)
        x = self.conv2(x, edge_index, edge_weight).relu()
        x = F.dropout(x, p=0.4, training=self.training)
        x = self.conv3(x, edge_index, edge_weight)  # 直接輸出回歸值
        return x.view(-1)  # 展平成一維
