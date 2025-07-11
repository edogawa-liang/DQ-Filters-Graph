import argparse
import torch
import numpy as np
import os
from data.dataset_loader import GraphDatasetLoader
from models.basic_GCN import GCN2Classifier, GCN3Classifier
from trainer.gnn_trainer import GNNClassifierTrainer
from utils.save_result import ExperimentLogger
from models.basic_mlp import MLPClassifier
from trainer.mlp_trainer import MLPClassifierTrainer
from data.feature2node import FeatureNodeConverter
from data.structure import StructureFeatureBuilder, extract_edges
from data.prepare_split import load_split_csv
from data.split_unknown_to_test import load_split_test
from utils.device import DEVICE

# 如果沒有 fix_train_valid，會 train/valid/test 每一次都重抽 (確定方法有效)
# 如果有 fix_train_valid，則固定 train/valid，只會重抽 test_mask (確定子圖適用於整體，並且用同一個模型)


def parse_args():
    parser = argparse.ArgumentParser(description="Train a GNN model with specified parameters.")
    parser.add_argument("--dataset", type=str, required=True, help="Dataset name")
    parser.add_argument("--normalize", action="store_true", help="Whether to normalize the dataset.")
    parser.add_argument("--model", type=str, default="GCN2", choices=["GCN2", "GCN3", "MLP"], help="Model type")
    parser.add_argument("--epochs", type=int, default=2, help="Number of training epochs")
    parser.add_argument("--lr", type=float, default=0.01, help="Learning rate")
    parser.add_argument("--weight_decay", type=float, default=1e-4, help="Weight decay for optimizer")
    parser.add_argument("--threshold", type=float, default=0.5, help="threshold for binary classification")
    
    # result settings
    parser.add_argument("--run_mode", type=str, default="try", help="Experiment run mode: 'try' (testing) or formal runs")
    parser.add_argument("--result_filename", type=str, default="result", help="File name for results document")
    parser.add_argument("--note", type=str, default="", help="Note for the experiment")
    parser.add_argument("--copy_old", type=lambda x: x.lower() == "true", default=True, help="Whether to backup old experiment data (true/false).")
    
    # only structure
    parser.add_argument("--only_structure", action="store_true", help="Use only structural information (all features set to 1)")

    # feature to node
    parser.add_argument("--feature_to_node", action="store_true", help="Convert features into nodes and edges.")

    # Only use feature-node (no node-node edges)
    parser.add_argument("--only_feature_node", action="store_true", help="Use only feature-node edges, no node-node edges.")
    
    # Structure Mode
    parser.add_argument("--structure_mode", type=str, default="random+imp", choices=["one+imp", "random+imp"], help="Mode for structure features: 'one' or 'random+imp'")

    # structure mode 是 "random+imp" 時，要不要使用 learnable embedding
    parser.add_argument("--learn_embedding", action="store_true", help="Use learnable random embedding")

    # repeat settings
    parser.add_argument("--repeat_start", type=int, default=0, help="Start repeat id (inclusive)")
    parser.add_argument("--repeat_end", type=int, default=9, help="End repeat id (inclusive)")

    # 是否固定 train/valid mask
    parser.add_argument("--fix_train_valid", action="store_true", help="If set, use fixed train/valid masks, only test mask varies by repeat_id.")

    # 使用的 split_id
    parser.add_argument("--split_id", type=int, default=0, help="Split ID to use for fixed train/valid masks (default: 0)")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    os.environ['TORCH'] = torch.__version__
    print(f"Using torch version: {torch.__version__}")
    print(f"Using device: {DEVICE}")

    # Load dataset
    loader = GraphDatasetLoader(args.normalize)
    data, num_features, _, feature_type, _ = loader.load_dataset(args.dataset)
    data = data.to(DEVICE)

    # define pad_mask function
    def pad_mask(mask, pad_len):
        return torch.cat([mask, torch.zeros(pad_len, dtype=torch.bool, device=mask.device)], dim=0)


    # 1. Load embedding if fix_train_valid + random+imp
    external_embedding = None

    # inference (只看structure) 階段才會 load external_embedding
    if args.fix_train_valid and (args.feature_to_node or args.only_structure) and args.structure_mode == "random+imp":        
        embedding_save_dir = os.path.join("saved", args.run_mode.replace(f"_split{args.split_id}", ""), "embedding", args.dataset)
        embedding_save_path = os.path.join(embedding_save_dir, f"{args.split_id}_embedding.npy")  # 使用的 split_id 的 embedding
        print(f"Loading embedding from {embedding_save_path}")
        embedding_np = np.load(embedding_save_path)
        print(f"Loaded embedding shape: {embedding_np.shape}")
        external_embedding = torch.tensor(embedding_np, device=DEVICE, dtype=torch.float)


    # 1. Feature to node conversion
    if args.feature_to_node:
        print("Converting node features into feature-nodes...")
        converter = FeatureNodeConverter(feature_type=feature_type, device=DEVICE)
        # FeatureNodeConverter 提供feature連到node後的所有邊，並多了 node_node_mask, node_feat_mask (後續要記得處理!)
        data = converter.convert(data)

    # 2. Structure feature building (node features)
    if args.feature_to_node or args.only_structure:
        print("Using StructureFeatureBuilder...")
        # 將 Feature 改成新的算法 (random (32 dim)+ [PageRank, Betweenness, Degree, Closeness])
        builder = StructureFeatureBuilder(
            data=data, device=DEVICE, dataset_name=args.dataset,
            feature_to_node=args.feature_to_node,
            only_feature_node=args.only_feature_node,
            only_structure=args.only_structure,
            mode=args.structure_mode,
            emb_dim=32,
            normalize_type="row_l1",
            learn_embedding=args.learn_embedding if external_embedding is None else False, # 在訓練階段時是 True, Inference 階段是 False
            external_embedding=external_embedding # 只有inference時才會有
        )
        structure_x = builder.build()
        num_features = structure_x.shape[1]
        data.x = structure_x

    # 3. Use original node features
    else:
        print("Using original graph and node features.")
        num_features = data.x.shape[1]

    # 統一更新 edge_index, edge_weight (不論原 graph 或 feature to node 都可以用 extract_edges)
    edge_index, edge_weight = extract_edges(data, args.feature_to_node, args.only_feature_node)
    data.edge_index = edge_index
    data.edge_weight = edge_weight

    # 如果固定 train/valid mask 在loop外面先載入
    if args.fix_train_valid:
        print("\nLoading fixed train/valid masks...")
        # 固定取 repeat_id = 0 的 train/valid mask
        train_mask, val_mask, _, unknown_mask = load_split_csv(args.dataset, args.split_id, DEVICE)
        num_orig_nodes = train_mask.shape[0]
        num_total_nodes = data.x.shape[0]

        # 如果有 feature_to_node 也一樣要 pad
        if args.feature_to_node and num_total_nodes > num_orig_nodes:
            pad_len = num_total_nodes - num_orig_nodes
            print(f"Padding fixed masks with {pad_len} additional nodes for feature nodes...")
            train_mask = pad_mask(train_mask)
            val_mask = pad_mask(val_mask)
            unknown_mask = pad_mask(unknown_mask)

        data.train_mask = train_mask
        data.val_mask = val_mask
        data.unknown_mask = unknown_mask



    # Repeat 10 time selecting different splits
    # 在 train/valid/test 情況下是 load 不同種組合，fix_train_valid情況下是 load 不同的 test
    for repeat_id in range(args.repeat_start, args.repeat_end + 1):

        print(f"\n===== [Repeat {repeat_id}] =====")

        if args.fix_train_valid:
            # 只 load test_mask
            print("fix train, Loading test_mask only...")
            test_mask = load_split_test(args.dataset, args.split_id, repeat_id, DEVICE)

            if args.feature_to_node and num_total_nodes > num_orig_nodes:
                test_mask = pad_mask(test_mask)

            data.test_mask = test_mask

        else:
            # Load the split mask
            train_mask, val_mask, test_mask, unknown_mask = load_split_csv(args.dataset, repeat_id, DEVICE) # 這裏的mask是原dataset的長度
            num_orig_nodes = train_mask.shape[0]
            num_total_nodes = data.x.shape[0]
            
            # add padding to feature node (mask會補滿，但y不會)
            if args.feature_to_node and num_total_nodes > num_orig_nodes:
                pad_len = num_total_nodes - num_orig_nodes
                print(f"Padding masks with {pad_len} additional nodes for feature nodes...")

                train_mask = pad_mask(train_mask)
                val_mask = pad_mask(val_mask)
                test_mask = pad_mask(test_mask)
                unknown_mask = pad_mask(unknown_mask) 

            data.train_mask = train_mask
            data.val_mask = val_mask
            data.test_mask = test_mask
            data.unknown_mask = unknown_mask


        # Initialize logger
        logger = ExperimentLogger(file_name=args.result_filename, note=args.note, copy_old=args.copy_old, run_mode=args.run_mode)

        # Loop over all modified graphs
        print(f"\nTraining on Graph - Split {repeat_id}")
        label_source = "Original Label"
        try:
            trial_number = logger.get_next_trial_number(args.dataset + "_classification")
            num_classes = len(torch.unique(data.y))
            print(f"Training Classification Model - Trial {trial_number}")
            
            if args.model == "MLP":
                trainer = MLPClassifierTrainer(dataset_name=args.dataset, data=data,
                                    num_features=num_features, num_classes=num_classes,
                                    model_class= MLPClassifier, 
                                    trial_number=trial_number, device=DEVICE,
                                    epochs=args.epochs, lr=args.lr, weight_decay=args.weight_decay,
                                    run_mode=args.run_mode, threshold=args.threshold)
            else: # GNN
                trainer = GNNClassifierTrainer(dataset_name=args.dataset, data=data, 
                                            num_features=num_features, num_classes=num_classes,  
                                            model_class=GCN2Classifier if args.model == "GCN2" else GCN3Classifier,
                                            trial_number=trial_number, device=DEVICE,
                                            epochs=args.epochs, lr=args.lr, weight_decay=args.weight_decay, 
                                            run_mode=args.run_mode, threshold=args.threshold,
                                            extra_params=[builder.embedding.weight] if (args.feature_to_node or args.only_structure) and args.structure_mode == "random+imp" and args.learn_embedding else None)
            
            if args.fix_train_valid: # 因目的是檢測不同的 test，所以固定模型
                # 因為這時的 run_mode 後面會+split_0，所以要去掉
                model_dir = os.path.join("saved", args.run_mode.replace(f"_split{args.split_id}", ""), "model", args.dataset)
                model_name = f"{args.split_id}_{trainer.model_name}.pth"  # 你的 save_model_path 是這個 pattern
                path_to_model = os.path.join(model_dir, model_name)
                print(f"Loading fixed model from {path_to_model}")
                trainer.load_model(path_to_model)
                # test 用固定模型
                result = trainer.test()

            else:
                result = trainer.run()


            logger.log_experiment(args.dataset + "_classification", result, label_source, repeat_id=repeat_id)

            # Save embedding
            # 即使不是 learn_embedding 也要保存 embedding。Inference 階段不需要存
            if (args.feature_to_node or args.only_structure) and args.structure_mode == "random+imp" and not args.fix_train_valid: 
                embedding_save_dir = os.path.join("saved", args.run_mode, "embedding", args.dataset)
                os.makedirs(embedding_save_dir, exist_ok=True)
                embedding_save_path = os.path.join(embedding_save_dir, f"{repeat_id}_embedding.npy")
                learned_embedding = builder.embedding.weight.detach().cpu().numpy()
                np.save(embedding_save_path, learned_embedding)
                print(f"[Repeat {repeat_id}] Saved embedding to {embedding_save_path}")
                
        except ValueError as e:
            raise