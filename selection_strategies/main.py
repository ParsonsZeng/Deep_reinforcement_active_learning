import utils
import datetime
import argparse
import torch

import train
from config import params, data, models, global_logger
from models import rnnae
from models.cnn_2 import CNN2

import uuid
import getpass
from logger import VisdomLogger



def main():
    parser = argparse.ArgumentParser(description="-----[CNN-classifier]-----")
    parser.add_argument("--similarity", default=0.0, type=float, help="similarity threshold")
    parser.add_argument("--similarity_representation", default="W2V", help="similarity representation. Available methods: CNN, AUTOENCODER, W2V")
    parser.add_argument("--mode", default="train",
                        help="train: train (with test) a model / test: test saved models")
    parser.add_argument("--model", default="cnn",
                        help="Type of model to use. Default: CNN. Available models: CNN, RNN")
    parser.add_argument("--embedding", default="w2v",
                        help="available embedings: random, w2v")
    parser.add_argument("--dataset", default="MR",
                        help="available datasets: MR, TREC")
    parser.add_argument("--encoder", default=None,
                        help="Path to encoder model file")
    parser.add_argument("--decoder", default=None,
                        help="Path to decoder model file")
    parser.add_argument('--batch-size', type=int, default=32,
                        help='batch size for training [default: 32]')
    parser.add_argument('--selection-size', type=int, default=32,
                        help='selection size for selection function [default: 32]')
    parser.add_argument("--save_model", default="F",
                        help="whether saving model or not (T/F)")
    parser.add_argument("--early_stopping", default="F",
                        help="whether to apply early stopping(T/F)")
    parser.add_argument("--epoch", default=100, type=int,
                        help="number of max epoch")
    parser.add_argument("--learning_rate", default=0.1,
                        type=float, help="learning rate")
    parser.add_argument("--dropout_embed", default=0.2,
                        type=float, help="Dropout embed probability. Default: 0.2")
    parser.add_argument("--dropout_model", default=0.4,
                        type=float, help="Dropout model probability. Default: 0.4")
    parser.add_argument('--device', type=int, default=0,
                        help='Cuda device to run on')
    parser.add_argument('--no-cuda', action='store_true',
                        default=False, help='disable the gpu')
    parser.add_argument("--scorefn", default="entropy",
                        help="available scoring functions: entropy, random, egl")
    parser.add_argument('--average', type=int, default=1,
                        help='Number of runs to average [default: 1]')
    parser.add_argument('--hnodes', type=int, default=256,
                        help='Number of nodes in the hidden layer(s)')
    parser.add_argument('--hlayers', type=int, default=1,
                        help='Number of hidden layers')
    parser.add_argument('--weight_decay', type=float, default=1e-5,
                        help='Value of weight_decay')
    parser.add_argument('--no-log', action='store_true',
                        default=False, help='Disable logging')
    parser.add_argument('--data_path',      default='/data/stud/jorgebjorn/data',       type=str,   help='Dir path to datasets')    
    parser.add_argument('--c',              default='',                                 type=str,   help='Comment to run ')    
 
    options = parser.parse_args()

    params["DATA_PATH"] = options.data_path #TODO rewrite?

    getattr(utils, "read_{}".format(options.dataset))()

    data["vocab"] = sorted(list(set(
        [w for sent in data["train_x"] + data["dev_x"]
            + data["test_x"] for w in sent])))
    data["classes"] = sorted(list(set(data["train_y"])))
    data["word_to_idx"] = {w: i for i, w in enumerate(data["vocab"])}

    params_local = {
        "SIMILARITY_THRESHOLD": options.similarity,
        "SIMILARITY_REPRESENTATION": options.similarity_representation,
        "DATA_PATH": options.data_path,
        "MODEL": options.model,
        "EMBEDDING": options.embedding,
        "DATASET": options.dataset,
        "SAVE_MODEL": bool(options.save_model == "T"),
        "EARLY_STOPPING": bool(options.early_stopping == "T"),
        "EPOCH": options.epoch,
        "LEARNING_RATE": options.learning_rate,
        "MAX_SENT_LEN": max([len(sent) for sent in data["train_x"]
                             + data["dev_x"] + data["test_x"]]),
        "SELECTION_SIZE": options.selection_size,
        "BATCH_SIZE": options.batch_size,
        "WORD_DIM": 300,
        "VOCAB_SIZE": len(data["vocab"]),
        "CLASS_SIZE": len(data["classes"]),
        "FILTERS": [3, 4, 5],
        "FILTER_NUM": [100, 100, 100],
        "DROPOUT_EMBED": options.dropout_embed,
        "DROPOUT_MODEL": options.dropout_model,
        "DEVICE": options.device,
        "NO_CUDA": options.no_cuda,
        "SCORE_FN": options.scorefn,
        "N_AVERAGE": options.average,
        "HIDDEN_SIZE": options.hnodes,
        "HIDDEN_LAYERS": options.hlayers,
        "WEIGHT_DECAY": options.weight_decay,
        "LOG": not options.no_log,
        "ENCODER": options.encoder,
        "DECODER": options.decoder,
        "C": options.c
    }

    for key in params_local:
        params[key] = params_local[key]

    if params["LOG"]:
        logger_name = 'SS/{}_{}_{}_{}_{}'.format(getpass.getuser(), datetime.datetime.now().strftime("%d-%m-%y_%H:%M"), options.dataset, params["C"], str(uuid.uuid4())[:4])
        global_logger["lg"] = VisdomLogger(logger_name)
        # global_logger["lg"].parameters_summary()
        print("visdom logger OK")
        # quit()

    params["CUDA"] = (not params["NO_CUDA"]) and torch.cuda.is_available()
    del params["NO_CUDA"]

    if params["CUDA"]:
        torch.cuda.set_device(params["DEVICE"])

    if params["EMBEDDING"] == "w2v":
        utils.load_word2vec()

    encoder = rnnae.EncoderRNN()
    # decoder = rnnae.DecoderRNN()
    decoder = rnnae.AttnDecoderRNN()
    feature_extractor = CNN2()

    if params["ENCODER"] != None:
        print("Loading encoder")
        encoder.load_state_dict(torch.load(params["ENCODER"]))

    if params["DECODER"] != None:
        print("Loading decoder")
        decoder.load_state_dict(torch.load(params["DECODER"]))

    if params["CUDA"]:
        encoder, decoder, feature_extractor = encoder.cuda(), decoder.cuda(), feature_extractor.cuda()

    models["ENCODER"] = encoder
    models["DECODER"] = decoder
    models["FEATURE_EXTRACTOR"] = feature_extractor

    print("=" * 20 + "INFORMATION" + "=" * 20)
    for key, value in params.items():
        print("{}: {}".format(key.upper(), value))

    if params["EMBEDDING"] == "random" and params["SIMILARITY_THRESHOLD"] > 0:
        print("********** WARNING *********")
        print("Random embedding makes similarity threshold have no effect. \n")

    print("=" * 20 + "TRAINING STARTED" + "=" * 20)
    train.active_train()
    print("=" * 20 + "TRAINING FINISHED" + "=" * 20)


if __name__ == "__main__":
    main()
