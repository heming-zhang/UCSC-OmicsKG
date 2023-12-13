import os
import pdb
import torch
import argparse
import tensorboardX
import numpy as np
import pandas as pd
import torch.nn as nn
from scipy import sparse
from torch.autograd import Variable

import utils
from geo_loader.read_geograph import read_batch
from geo_loader.geograph_sampler import GeoGraphLoader
from enc_dec.geo_gcn_decoder import GCNDecoder
from sklearn.metrics import accuracy_score, f1_score, confusion_matrix, precision_score, recall_score

# Parse arguments from command line
def arg_parse():
    parser = argparse.ArgumentParser(description='COEMBED ARGUMENTS.')
    # Add following arguments
    parser.add_argument('--cuda', dest = 'cuda',
                help = 'CUDA.')
    parser.add_argument('--parallel', dest = 'parallel',
                help = 'Parrallel Computing')
    parser.add_argument('--GPU IDs', dest = 'gpu_ids',
                help = 'GPU IDs')
    parser.add_argument('--add-self', dest = 'adj_self',
                help = 'Graph convolution add nodes themselves.')
    parser.add_argument('--model', dest = 'model',
                help = 'Model load.')
    parser.add_argument('--lr', dest = 'lr', type = float,
                help = 'Learning rate.')
    parser.add_argument('--batch-size', dest = 'batch_size', type = int,
                help = 'Batch size.')
    parser.add_argument('--num_workers', dest = 'num_workers', type = int,
                help = 'Number of workers to load data.')
    parser.add_argument('--epochs', dest = 'num_epochs', type = int,
                help = 'Number of epochs to train.')
    parser.add_argument('--input-dim', dest = 'input_dim', type = int,
                help = 'Input feature dimension')
    parser.add_argument('--hidden-dim', dest = 'hidden_dim', type = int,
                help = 'Hidden dimension')
    parser.add_argument('--output-dim', dest = 'output_dim', type = int,
                help = 'Output dimension')
    parser.add_argument('--num-gc-layers', dest = 'num_gc_layers', type = int,
                help = 'Number of graph convolution layers before each pooling')
    parser.add_argument('--dropout', dest = 'dropout', type = float,
                help = 'Dropout rate.')

    # Set default input argument
    parser.set_defaults(cuda = '0',
                        parallel = False,
                        add_self = '0', # 'add'
                        model = '0', # 'load'
                        lr = 0.01,
                        clip = 2.0,
                        batch_size = 768,
                        num_workers = 1,
                        num_epochs = 100,
                        input_dim = 8,
                        hidden_dim = 24,
                        output_dim = 24,
                        dropout = 0.01)
    return parser.parse_args()


def learning_rate_schedule(args, dl_input_num, iteration_num, e1, e2, e3, e4):
    epoch_iteration = int(dl_input_num / args.batch_size)
    l1 = (args.lr - 0.008) / (e1 * epoch_iteration)
    l2 = (0.008 - 0.006) / (e2 * epoch_iteration)
    l3 = (0.006 - 0.005) / (e3 * epoch_iteration)
    l4 = (0.005 - 0.001) / (e4 * epoch_iteration)
    l5 = 0.001
    if iteration_num <= (e1 * epoch_iteration):
        learning_rate = args.lr - iteration_num * l1
    elif iteration_num <= (e1 + e2) * epoch_iteration:
        learning_rate = 0.008 - (iteration_num - e1 * epoch_iteration) * l2
    elif iteration_num <= (e1 + e2 + e3) * epoch_iteration:
        learning_rate = 0.006 - (iteration_num - (e1 + e2) * epoch_iteration) * l3
    elif iteration_num <= (e1 + e2 + e3 + e4) * epoch_iteration:
        learning_rate = 0.005 - (iteration_num - (e1 + e2 + e3) * epoch_iteration) * l4
    else:
        learning_rate = l5
    print('-------LEARNING RATE: ' + str(learning_rate) + '-------' )
    return learning_rate


def build_geogcn_model(args, device, graph_output_folder):
    print('--- BUILDING UP GCN MODEL ... ---')
    # Get parameters
    # [num_gene, (adj)node_num]
    final_annotation_gene_df = pd.read_csv(os.path.join(graph_output_folder, 'map-all-gene.csv'))
    gene_name_list = list(final_annotation_gene_df['Gene_name'])
    node_num = len(gene_name_list)
    # [num_edge]
    gene_num_edge_df = pd.read_csv(os.path.join(graph_output_folder, 'merged-gene-edge-num-all.csv'))
    num_edge = gene_num_edge_df.shape[0]
    # import pdb; pdb.set_trace()
    # Build up model
    model = GCNDecoder(input_dim=args.input_dim, hidden_dim=args.hidden_dim, embedding_dim=args.output_dim, 
                node_num=node_num, device=device)
    model = model.to(device)
    return model


def train_geogcn_model(dataset_loader, model, device, args, learning_rate):
    optimizer = torch.optim.Adam(filter(lambda p : p.requires_grad, model.parameters()), lr=learning_rate, eps=1e-7, weight_decay=1e-6)
    batch_loss = 0
    for batch_idx, data in enumerate(dataset_loader):
        optimizer.zero_grad()
        x = Variable(data.x.float(), requires_grad=False).to(device)
        edge_index = Variable(data.edge_index, requires_grad=False).to(device)
        label = Variable(data.label, requires_grad=False).to(device)
        output, ypred = model(x, edge_index)
        loss = model.loss(output, label)
        loss.backward()
        batch_loss += loss.item()
        nn.utils.clip_grad_norm_(model.parameters(), args.clip)
        optimizer.step()
    return model, batch_loss, ypred


def train_geogcn(args, fold_n, load_path, iteration_num, device, graph_output_folder):
    # Training dataset basic parameters
    # [num_feature, num_node]
    num_feature = 8
    final_annotation_gene_df = pd.read_csv(os.path.join(graph_output_folder, 'map-all-gene.csv'))
    gene_name_list = list(final_annotation_gene_df['Gene_name'])
    num_node = len(gene_name_list)
    form_data_path = './' + graph_output_folder + '/form_data'
    # Read these feature label files
    print('--- LOADING TRAINING FILES ... ---')
    xTr = np.load(form_data_path + '/xTr' + str(fold_n) + '.npy')
    yTr = np.load(form_data_path + '/yTr' + str(fold_n) + '.npy')
    edge_index = torch.from_numpy(np.load(form_data_path + '/edge_index.npy') ).long() 

    # Build [WeightBiGNN, DECODER] model
    model = build_geogcn_model(args, device, graph_output_folder)
    if args.model == 'load':
        model.load_state_dict(torch.load(load_path, map_location=device))

    # Train model on training dataset
    # Other parameters
    dl_input_num = xTr.shape[0]
    epoch_num = args.num_epochs
    learning_rate = args.lr
    batch_size = args.batch_size
    # Record epoch loss and accuracy correlation
    if args.model != 'load':
        iteration_num = 0
    max_test_corr = 0
    max_test_corr_id = 0
    e1 = 10
    e2 = 10
    e3 = 10
    e4 = 30
    epoch_loss_list = []
    epoch_acc_list = []
    test_loss_list = []
    test_acc_list = []
    # Clean result previous epoch_i_pred files
    folder_name = 'epoch_' + str(epoch_num)
    path = './result/%s' % (folder_name)
    unit = 1
    while os.path.exists('./result') == False:
        os.mkdir('./result')
    while os.path.exists(path):
        path = './result/%s_%d' % (folder_name, unit)
        unit += 1
    os.mkdir(path)
    # import pdb; pdb.set_trace()
    for i in range(1, epoch_num + 1):
        print('---------------------------EPOCH: ' + str(i) + ' ---------------------------')
        print('---------------------------EPOCH: ' + str(i) + ' ---------------------------')
        print('---------------------------EPOCH: ' + str(i) + ' ---------------------------')
        print('---------------------------EPOCH: ' + str(i) + ' ---------------------------')
        print('---------------------------EPOCH: ' + str(i) + ' ---------------------------')
        model.train()
        epoch_ypred = np.zeros((1, 1))
        upper_index = 0
        batch_loss_list = []
        dl_input_num = xTr.shape[0]
        for index in range(0, dl_input_num, batch_size):
            if (index + batch_size) < dl_input_num:
                upper_index = index + batch_size
            else:
                upper_index = dl_input_num
            geo_datalist = read_batch(index, upper_index, xTr, yTr, num_feature, num_node, edge_index, graph_output_folder)
            dataset_loader, node_num, feature_dim = GeoGraphLoader.load_graph(geo_datalist, prog_args)
            # Activate learning rate schedule
            iteration_num += 1
            learning_rate = learning_rate_schedule(args, dl_input_num, iteration_num, e1, e2, e3, e4)
            print('TRAINING MODEL...')
            model, batch_loss, batch_ypred = train_geogcn_model(dataset_loader, model, device, args, learning_rate)
            print('BATCH LOSS: ', batch_loss)
            batch_loss_list.append(batch_loss)
            # Preserve prediction of batch training data
            batch_ypred = (Variable(batch_ypred).data).cpu().numpy().reshape(-1, 1)
            epoch_ypred = np.vstack((epoch_ypred, batch_ypred))
        epoch_loss = np.mean(batch_loss_list)
        print('TRAIN EPOCH ' + str(i) + ' MSE LOSS: ', epoch_loss)
        epoch_loss_list.append(epoch_loss)
        epoch_ypred = np.delete(epoch_ypred, 0, axis = 0)
        print(epoch_ypred)
        print('ITERATION NUMBER UNTIL NOW: ' + str(iteration_num))
        # Preserve acc corr for every epoch
        score_lists = list(yTr)
        score_list = [item for elem in score_lists for item in elem]
        epoch_ypred_lists = list(epoch_ypred)
        epoch_ypred_list = [item for elem in epoch_ypred_lists for item in elem]
        train_dict = {'label': score_list, 'prediction': epoch_ypred_list}
        tmp_training_input_df = pd.DataFrame(train_dict)
        # pdb.set_trace()
        # Calculating metrics
        accuracy = accuracy_score(tmp_training_input_df['label'], tmp_training_input_df['prediction'])
        f1 = f1_score(tmp_training_input_df['label'], tmp_training_input_df['prediction'])
        precision = precision_score(tmp_training_input_df['label'], tmp_training_input_df['prediction'])
        recall = recall_score(tmp_training_input_df['label'], tmp_training_input_df['prediction'])  # Sensitivity
        tn, fp, fn, tp = confusion_matrix(tmp_training_input_df['label'], tmp_training_input_df['prediction']).ravel()
        specificity = tn / (tn+fp) if (tn+fp) != 0 else 0
        epoch_acc_list.append(accuracy)
        tmp_training_input_df.to_csv(path + '/TrainingPred_' + str(i) + '.txt', index=False, header=True)
        print('EPOCH ' + str(i) + ' ACCURACY: ', accuracy)
        print('EPOCH ' + str(i) + ' F1: ', f1)
        print('EPOCH ' + str(i) + ' PRECISION: ', precision)
        print('EPOCH ' + str(i) + ' RECALL: ', recall)
        print('EPOCH ' + str(i) + ' SPECIFICITY: ', specificity)
        print('\n-------------EPOCH TRAINING ACCURACY LIST: -------------')
        print(epoch_acc_list)
        print('\n-------------EPOCH TRAINING LOSS LIST: -------------')
        print(epoch_loss_list)

        # # # Test model on test dataset
        # fold_n = 1
        test_save_path = path
        test_acc, test_loss, tmp_test_input_df = test_geogcn(prog_args, fold_n, model, test_save_path, device, graph_output_folder)
        test_acc_list.append(test_acc)
        test_loss_list.append(test_loss)
        tmp_test_input_df.to_csv(path + '/TestPred' + str(i) + '.txt', index=False, header=True)
        print('\n-------------EPOCH TEST ACCURACY LIST: -------------')
        print(test_acc_list)
        print('\n-------------EPOCH TEST MSE LOSS LIST: -------------')
        print(test_loss_list)
        # SAVE BEST TEST MODEL
        if test_acc > max_test_acc:
            max_test_acc = test_acc
            max_test_acc_id = i
            # torch.save(model.state_dict(), path + '/best_train_model'+ str(i) +'.pt')
            torch.save(model.state_dict(), path + '/best_train_model.pt')
        print('\n-------------BEST TEST ACCURACY MODEL ID INFO:' + str(max_test_corr_id) + '-------------')
        print('--- TRAIN ---')
        print('BEST MODEL TRAIN LOSS: ', epoch_loss_list[max_test_corr_id - 1])
        print('BEST MODEL TRAIN ACCURACY: ', epoch_acc_list[max_test_corr_id - 1])
        print('--- TEST ---')
        print('BEST MODEL TEST LOSS: ', test_loss_list[max_test_corr_id - 1])
        print('BEST MODEL TEST ACCURACY: ', test_acc_list[max_test_corr_id - 1])
        torch.save(model.state_dict(), path + '/best_train_model.pt')


def test_geogcn_model(dataset_loader, model, device, args):
    batch_loss = 0
    for batch_idx, data in enumerate(dataset_loader):
        x = Variable(data.x, requires_grad=False).to(device)
        edge_index = Variable(data.edge_index, requires_grad=False).to(device)
        label = Variable(data.label, requires_grad=True).to(device)
        # This will use method [def forward()] to make prediction
        output, ypred = model(x, edge_index)
        loss = model.loss(output, label)
        batch_loss += loss.item()
    return model, batch_loss, ypred


def test_geogcn(args, fold_n, model, test_save_path, device, graph_output_folder):
    print('-------------------------- TEST START --------------------------')
    print('-------------------------- TEST START --------------------------')
    print('-------------------------- TEST START --------------------------')
    print('-------------------------- TEST START --------------------------')
    print('-------------------------- TEST START --------------------------')
    # Test model on test dataset
    form_data_path = './' + graph_output_folder + '/form_data'
    xTe = np.load(form_data_path + '/xTe' + str(fold_n) + '.npy')
    yTe = np.load(form_data_path + '/yTe' + str(fold_n) + '.npy')
    edge_index = torch.from_numpy(np.load(form_data_path + '/edge_index.npy') ).long() 

    dl_input_num = xTe.shape[0]
    batch_size = args.batch_size
    # Clean result previous epoch_i_pred files
    path = test_save_path
    # [num_feature, num_node]
    num_feature = 8
    final_annotation_gene_df = pd.read_csv(os.path.join(graph_output_folder, 'map-all-gene.csv'))
    gene_name_list = list(final_annotation_gene_df['Gene_name'])
    num_node = len(gene_name_list)
    # Run test model
    model.eval()
    all_ypred = np.zeros((1, 1))
    upper_index = 0
    batch_loss_list = []
    for index in range(0, dl_input_num, batch_size):
        if (index + batch_size) < dl_input_num:
            upper_index = index + batch_size
        else:
            upper_index = dl_input_num
        geo_datalist = read_batch(index, upper_index, xTe, yTe, num_feature, num_node, edge_index, graph_output_folder)
        dataset_loader, node_num, feature_dim = GeoGraphLoader.load_graph(geo_datalist, args)
        print('TEST MODEL...')
        # import pdb; pdb.set_trace()
        model, batch_loss, batch_ypred = test_geogcn_model(dataset_loader, model, device, args)
        print('BATCH LOSS: ', batch_loss)
        batch_loss_list.append(batch_loss)
        # Preserve prediction of batch test data
        batch_ypred = (Variable(batch_ypred).data).cpu().numpy().reshape(-1, 1)
        all_ypred = np.vstack((all_ypred, batch_ypred))
    test_loss = np.mean(batch_loss_list)
    print('MSE LOSS: ', test_loss)
    # Preserve accuracy for every epoch
    all_ypred = np.delete(all_ypred, 0, axis = 0)
    all_ypred_lists = list(all_ypred)
    all_ypred_list = [item for elem in all_ypred_lists for item in elem]
    score_lists = list(yTe)
    score_list = [item for elem in score_lists for item in elem]
    test_dict = {'label': score_list, 'prediction': all_ypred_list}
    tmp_test_input_df = pd.DataFrame(test_dict)
    # Calculating metrics
    accuracy = accuracy_score(tmp_test_input_df['label'], tmp_test_input_df['prediction'])
    f1 = f1_score(tmp_test_input_df['label'], tmp_test_input_df['prediction'])
    precision = precision_score(tmp_test_input_df['label'], tmp_test_input_df['prediction'])
    recall = recall_score(tmp_test_input_df['label'], tmp_test_input_df['prediction'])  # Sensitivity
    tn, fp, fn, tp = confusion_matrix(tmp_test_input_df['label'], tmp_test_input_df['prediction']).ravel()
    specificity = tn / (tn+fp) if (tn+fp) != 0 else 0

    test_pearson = tmp_test_input_df.corr(method = 'pearson')
    print('PEARSON CORRELATION: ', test_pearson)
    print('FOLD - ', fold_n)
    return test_pearson, test_loss, tmp_test_input_df


if __name__ == "__main__":
    # Parse argument from terminal or default parameters
    prog_args = arg_parse()

    # Check and allocate resources
    device, prog_args.gpu_ids = utils.get_available_devices()
    # Manual set
    device = torch.device('cuda:0') 
    torch.cuda.set_device(device)
    print('MAIN DEVICE: ', device)
    # Single gpu
    prog_args.gpu_ids = [0]
    os.environ["CUDA_VISIBLE_DEVICES"] = '0'
    
    ### Train the model
    # Train [FOLD-1x]
    fold_n = 1
    # prog_args.model = 'load'
    # load_path = './result/epoch_60_1/best_train_model.pt'
    load_path = ''
    graph_output_folder = 'graph-data'
    yTr = np.load('./' + graph_output_folder + '/form_data/yTr' + str(fold_n) + '.npy')
    # yTr = np.load('./' + graph_output_folder + '/form_data/y_split1.npy')
    dl_input_num = yTr.shape[0]
    epoch_iteration = int(dl_input_num / prog_args.batch_size)
    start_iter_num = 100 * epoch_iteration
    train_geogcn(prog_args, fold_n, load_path, start_iter_num, device, graph_output_folder)

    # ### Test the model
    # # TEST [FOLD-1]
    # fold_n = 1
    # model = build_geogcn_model(prog_args, device, graph_output_folder)
    # test_load_path = './' + graph_output_folder + '/result/epoch_60/best_train_model.pt'
    # model.load_state_dict(torch.load(test_load_path, map_location=device))
    # test_save_path = './' + graph_output_folder + '/result/epoch_60'
    # test_geogcn(prog_args, fold_n, model, test_save_path, device, dataset)