import torch
from torch_cluster import random_walk
from collections import defaultdict

from subgraph_selector.utils.choose_node import ChooseNodeSelector

# 目前的random walk 沒有考慮 edge weight
class RandomWalkEdgeSelector:
    """
    Selects the top fraction of most frequently visited edges (by random walk) for removal.
    """

    def __init__(self, data, node_ratio="auto", edge_ratio=0.5, fraction=0.1,  
                 walk_length=10, num_walks=5, node_choose="top_pagerank", feature_type="categorical", device="cpu", manual_nodes=None, mask_type="train", top_k_percent_feat=0.1, ori_data=None):
        """
        :param data: PyG graph data
        :param node_ratio: "auto" for automatic calculation or a numeric value to manually set node selection ratio
        :param edge_ratio: Ensures sufficient edges in the subgraph, required only if node_ratio is 'auto'
        :param fraction: Fraction of most frequently visited edges to remove (0.1 → top 10%)
        :param walk_length: Number of steps per random walk
        :param num_walks: Number of random walks per starting node
        :param node_choose: Strategy for selecting starting nodes ("random", "high_degree", "top_pagerank", "manual")
        :param device: Device to use (e.g., 'cpu' or 'cuda')
        :param manual_nodes: Manually specified nodes if node_choose is "manual"
        """
        self.data = data
        self.node_ratio = node_ratio
        self.edge_ratio = edge_ratio
        self.fraction = fraction
        self.walk_length = walk_length
        self.num_walks = num_walks
        self.node_choose = node_choose
        self.feature_type = feature_type  # 用於選擇特徵邊
        self.device = device
        self.manual_nodes = manual_nodes
        self.mask_type = mask_type
        self.top_k_percent_feat = top_k_percent_feat
        self.ori_data = ori_data

        self._get_start_nodes()
        self._calculate_neighbor_edges()


    def _get_start_nodes(self):
        """
        Uses `ChooseNodeSelector` to select the starting nodes based on the given strategy.
        """
        node_selector = ChooseNodeSelector(self.ori_data, node_ratio=self.node_ratio, edge_ratio=self.edge_ratio,
                                        strategy=self.node_choose, manual_nodes=self.manual_nodes, mask_type=self.mask_type)

        selected_nodes = node_selector.select_nodes() # 因為特徵節點接在普通節點後，可以直接把ori data 的 node_indices 當成新 data 要解釋的indices
        self.start_nodes = torch.tensor(selected_nodes, dtype=torch.long, device=self.device)
        self.final_node_ratio = len(selected_nodes) / self.data.x.shape[0]

    def _calculate_neighbor_edges(self):
        """
        計算選中的節點的鄰居邊數量。
        """
        edge_index = self.data.edge_index
        selected_set = set(self.start_nodes.tolist())
        self.neighbor_edges = [
            (src, dst) for src, dst in edge_index.t().tolist() if src in selected_set or dst in selected_set
        ]
        self.neighbor_edge_ratio = len(self.neighbor_edges)/self.data.edge_index.shape[1]

    def get_final_node_ratio(self):
        return self.final_node_ratio
    
    def get_neighbor_edge_ratio(self):
        return self.neighbor_edge_ratio
       

    def select_edges(self, return_feat_ids=False):
        """
        1. Perform random walks using PyG's `random_walk()`.
        2. Count how many times each edge is visited.
        3. Remove the top `self.fraction` fraction of edges (by frequency).
        4. Return indices of these edges in the original `edge_index`.
        """
        edge_index = self.data.edge_index.to(self.device)

        # 檢查圖是否為無向圖
        is_undirected = self.data.is_undirected()
        print(f"Graph is {'Undirected' if is_undirected else 'Directed'}.")

        # 執行 random_walk
        walks = random_walk(
            edge_index[0], 
            edge_index[1], 
            start=self.start_nodes.repeat_interleave(self.num_walks), 
            walk_length=self.walk_length
        )
        print(f"Shape of walks: {walks.shape}")

        # 統計每條邊被訪問的次數
        visited_edge_count = defaultdict(int)

        # 走訪結果可能是 2D tensor (num_walks_total, walk_length+1)
        for walk in walks:
            # walk 可能是 1D
            # 先確保它是 list or 1D
            if walk.dim() == 0:
                # 單個 scalar，無法形成路徑
                continue
            # walk 是 1D tensor，表示一條完整的 random walk
            for i in range(len(walk) - 1):
                src = walk[i].item()
                dst = walk[i+1].item()
                if src != dst:  # 避免 self-loop
                    if is_undirected:
                        edge = (min(src, dst), max(src, dst))
                    else:
                        edge = (src, dst)
                    visited_edge_count[edge] += 1


        # 根據出現次數排序，取前 self.fraction 比例
        all_edges = list(visited_edge_count.items())  # [(edge, freq), ...]
        all_edges.sort(key=lambda x: x[1], reverse=True)

        total_all_edges = len(all_edges)
        print(f"Total edges explored (all_edges): {total_all_edges}")


        # 建立 (src, dst) → edge_index 映射
        edge_map = {
            tuple(edge.tolist()): idx for idx, edge in enumerate(edge_index.t())
        }

        num_total_edges = edge_index.shape[1]
        num_ori_edges = self.ori_data.edge_index.shape[1] if self.ori_data is not None else num_total_edges
        # print("Total edges in original data:", num_ori_edges)
        num_feat = num_total_edges - num_ori_edges

        selected_ori = []
        selected_feat = []

        for edge, _ in all_edges:
            if edge in edge_map:
                idx = edge_map[edge]
                if idx < num_ori_edges:
                    selected_ori.append(idx)
                else:
                    selected_feat.append(idx)


        num_visited_ori_edges = len(selected_ori)
        print(f"走過的原始邊數量: {num_visited_ori_edges}")
        num_visited_feat_edges = len(selected_feat)
        print(f"走過的特徵邊數量: {num_visited_feat_edges}")
        
        # 按比例選擇
        num_selected_ori = int(num_ori_edges * self.fraction)
        # selected_ori = selected_ori[:num_selected_ori]
        print(f"應挑 {num_selected_ori} original edges (fraction {self.fraction})")
        print(f"實際走的原始邊比例: {num_visited_ori_edges / num_ori_edges * 100:.2f}%")
        
        # 如果選擇的原始邊數量少於應挑的數量，則從剩餘的原始邊中補齊
        if len(selected_ori) < num_selected_ori:
            print(f" 探索到的原始邊只有 {len(selected_ori)}，少於應挑的 {num_selected_ori}，將從其他原始邊補齊。")
            # 找出還沒被走過的原始邊 index
            remaining_ori = list(set(range(num_ori_edges)) - set(selected_ori))
            needed_ori = num_selected_ori - len(selected_ori)
            if len(remaining_ori) >= needed_ori:
                additional_ori = torch.tensor(remaining_ori, device=self.device)[torch.randperm(len(remaining_ori))[:needed_ori]].tolist()
                selected_ori += additional_ori
            else:
                print(f"可補充的原始邊不足，只補上 {len(remaining_ori)} 條。")
                selected_ori += remaining_ori
        else:
            selected_ori = selected_ori[:num_selected_ori]


              
        ori_num_features = self.ori_data.x.size(1) if self.ori_data is not None else None

        if num_feat > 0:
            if self.feature_type == "categorical":
                num_selected_feat = int(num_feat * self.top_k_percent_feat)
            elif self.feature_type == "continuous":
                num_selected_feat = int(ori_num_features * self.top_k_percent_feat * self.ori_data.num_nodes)  # 特徵數量 × top_k_percent_feat × 節點數 (類別型的話會超過實際的特徵邊數..)

            print(f"應挑 {num_selected_feat} feature edges (top {self.top_k_percent_feat * 100}%)")
            print(f"實際走的特徵邊比例: {num_visited_feat_edges / num_feat * 100:.2f}%")

            # 如果選擇的特徵邊數量少於應挑的數量，則從剩餘的特徵邊中補齊
            if len(selected_feat) < num_selected_feat:
                print(f"探索到的特徵邊只有 {len(selected_feat)}，少於應挑的 {num_selected_feat}，將從其他特徵邊補齊。")
                remaining_feat = list(range(num_ori_edges, num_total_edges))
                remaining_feat = list(set(remaining_feat) - set(selected_feat))
                needed_feat = num_selected_feat - len(selected_feat)
                if len(remaining_feat) >= needed_feat:
                    additional_feat = torch.tensor(remaining_feat, device=self.device)[torch.randperm(len(remaining_feat))[:needed_feat]].tolist()
                    selected_feat += additional_feat
                else:
                    print(f"可補充的特徵邊不足，只補上 {len(remaining_feat)} 條。")
                    selected_feat += remaining_feat
            else:
                selected_feat = selected_feat[:num_selected_feat]

            # selected_feat = selected_feat[:num_selected_feat]

        else:
            selected_feat = []
            print("No feature edges found.")

        selected_indices = selected_ori + selected_feat
        selected_tensor = torch.tensor(selected_indices, dtype=torch.long, device=self.device)

        ori_edge_visit_ratio = num_visited_ori_edges / num_ori_edges if num_ori_edges > 0 else 0
        feat_edge_visit_ratio = num_visited_feat_edges / num_feat if num_feat > 0 else 0


        if return_feat_ids:
            if ori_num_features is None:
                raise ValueError("num_features must be provided when return_feat_ids=True.")

            selected_feat_ids = []
            for idx in selected_feat:
                rel_idx = idx - num_ori_edges
                pair_id = rel_idx // 2
                feat_id = pair_id % ori_num_features
                selected_feat_ids.append(feat_id)

            return selected_tensor, selected_feat_ids, ori_edge_visit_ratio, feat_edge_visit_ratio

        return selected_tensor, [], ori_edge_visit_ratio, feat_edge_visit_ratio