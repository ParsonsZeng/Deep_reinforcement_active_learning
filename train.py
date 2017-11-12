import copy
import logger
import datetime

from torch.autograd import Variable
from sklearn.utils import shuffle

import torch
import torch.optim as optim
import torch.nn as nn

from models.cnn import CNN
from models.rnn import RNN
from selection_strategies import select_random, select_entropy, select_egl, select_all


def to_np(x):
    return x.data.cpu().numpy()


def active_train(data, params):
    init_learning_rate = params["LEARNING_RATE"]
    average_accs = {}
    average_losses = {}

    if params["MODEL"] == "cnn":
        model = CNN(data, params)
    elif params["MODEL"] == "rnn":
        model = RNN(params, data)
    else:
        model = CNN(data, params)

    if params["CUDA"]:
        model.cuda()

    for j in range(params["N_AVERAGE"]):
        params["LEARNING_RATE"] = init_learning_rate
        lg = init_logger(params, j)
        lg.scalar_summary("test-acc", 50, 0)

        print("-" * 20, "Round {}".format(j), "-" * 20)
        model.init_model()
        train_features = []
        train_targets = []

        data["train_x"], data["train_y"] = shuffle(data["train_x"], data["train_y"])

        n_rounds = int(500 / params["BATCH_SIZE"])
        for i in range(n_rounds):

            print("Unlabeled pool size: {}".format(len(data["train_x"])))
            print("Learning rate: {}".format(params["LEARNING_RATE"]))

            if params["SCORE_FN"] == "all":
                t1, t2 = select_all(model, data, params)
            elif i == 0:
                t1, t2 = select_random(model, data, params)
            else:
                if params["SCORE_FN"] == "entropy":
                    t1, t2 = select_entropy(model, data, params)
                elif params["SCORE_FN"] == "egl":
                    t1, t2 = select_egl(model, data, params)
                elif params["SCORE_FN"] == "random":
                    t1, t2 = select_random(model, data, params)

            train_features.extend(t1)
            train_targets.extend(t2)

            print("\n")
            model.init_model()
            model = train(model, params, train_features, train_targets, data, lg)
            accuracy, loss = evaluate(data, model, params, lg, i, mode="dev")
            if i not in average_accs:
                average_accs[i] = [accuracy]
            else:
                average_accs[i].append(accuracy)

            if i not in average_losses:
                average_losses[i] = [loss]

            else:
                average_losses[i].append(loss)

            print("New  accuracy: {}".format(sum(average_accs[i]) / len(average_accs[i])))
            lg.scalar_summary("test-acc", sum(average_accs[i]) / len(average_accs[i]), len(train_features))
            lg.scalar_summary("test-loss", sum(average_losses[i]) / len(average_losses[i]), len(train_features))
            log_model(model, lg)

    best_model = {}
    return best_model


def train(model, params, train_features, train_targets, data, lg):
    print("Labeled pool size: {}".format(len(train_features)))

    parameters = filter(lambda p: p.requires_grad, model.parameters())

    if params["MODEL"] == "rnn":
        optimizer = optim.Adadelta(parameters, params["LEARNING_RATE"], weight_decay=params["WEIGHT_DECAY"])
    else:
        optimizer = optim.Adadelta(parameters, params["LEARNING_RATE"])

    criterion = nn.CrossEntropyLoss()
    model.train()

    best_model = None
    best_acc = 0
    best_epoch = 0

    for e in range(params["EPOCH"]):
        shuffle(train_features, train_targets)
        avg_loss = 0
        corrects = 0

        for i in range(0, len(train_features), params["BATCH_SIZE"]):
            batch_range = min(params["BATCH_SIZE"], len(train_features) - i)
            batch_x = train_features[i:i + batch_range]
            batch_y = train_targets[i:i + batch_range]

            feature = Variable(torch.LongTensor(batch_x))
            target = Variable(torch.LongTensor(batch_y))
            if params["CUDA"]:
                feature, target = feature.cuda(params["DEVICE"]), target.cuda(params["DEVICE"])

            optimizer.zero_grad()
            pred = model(feature)
            loss = criterion(pred, target)
            loss.backward()
            optimizer.step()
            avg_loss += loss.data[0]
            new_corr = (torch.max(pred, 1)[1].view(target.size()).data == target.data).sum()
            corrects += new_corr

        print("Training process: {0:.0f}% completed ".format(100 * (e / params["EPOCH"])), end="\r")


        if params["SCORE_FN"] == "all":
            evaluate(data, model, params, lg, e, mode="dev")
        elif ((e + 1) % 10) == 0:
            avg_loss = avg_loss * params["BATCH_SIZE"] / len(train_features)
            size = len(train_features)
            accuracy = 100.0 * corrects / size
            print('{}: Evaluation - loss: {:.6f}  acc: {:.4f}%({}/{})'.format("train", avg_loss, accuracy, corrects, size))
            eval_acc, eval_loss = evaluate(data, model, params, lg, e, mode="dev")

            # TODO check if this should also apply for cnn
            if eval_acc > best_acc:
                print("New best model at epoch {}".format(e + 1))
                best_acc = eval_acc
                best_model = copy.deepcopy(model)
                best_epoch = e


    # WIMSEN ADAPTIVE LEARNING RATE
    if best_epoch < 60 and params["MODEL"] == "rnn":
        params["LEARNING_RATE"] = params["LEARNING_RATE"] * 0.65

    # return best_model if best_model != None else model
    return best_model


def init_logger(params, average):
    if params["MODEL"] == "cnn":
        lg = logger.Logger('./logs/cnn/batch_size={},date={},FILTERS={},FILTER_NUM={},WORD_DIM={},MODEL={},DROPOUT_PROB={},SCORE_FN={},AVERAGE={}'.format(
            str(params["BATCH_SIZE"]),
            datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S'),
            str(params["FILTERS"]),
            str(params["FILTER_NUM"]),
            str(params["WORD_DIM"]),
            str(params["MODEL"]),
            str(params["DROPOUT_PROB"]),
            str(params["SCORE_FN"]),
            # str(params["N_AVERAGE"])
            str(average + 1)
        ))

    if (params["MODEL"]=="rnn"):
        lg = logger.Logger('./logs/rnn/batch_size={},date={},WORD_DIM={},MODEL={},DROPOUT_PROB={},SCORE_FN={},AVERAGE={},LEARNING_RATE={},WEIGHT_DECAY={}'.format(
            str(params["BATCH_SIZE"]),
            datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S'),
            str(params["WORD_DIM"]),
            str(params["MODEL"]),
            str(params["DROPOUT_PROB"]),
            str(params["SCORE_FN"]),
            # str(params["N_AVERAGE"]),
            str(average + 1),
            str(params["LEARNING_RATE"]),
            str(params["WEIGHT_DECAY"])
        ))
    return lg


def log_model(model, lg):
    for tag, value in model.named_parameters():
        if value.requires_grad and hasattr(value.grad, "data"):
            tag = tag.replace('.', '/')
            lg.histo_summary(tag, to_np(value), step + 1)
            lg.histo_summary(tag + '/grad', to_np(value.grad), step + 1)

def evaluate(data, model, params, lg, step, mode="test"):
    model.eval()

    if params["CUDA"]:
        model.cuda()

    corrects, avg_loss = 0, 0
    for i in range(0, len(data["{}_x".format(mode)]), params["BATCH_SIZE"]):
        batch_range = min(params["BATCH_SIZE"], len(data["{}_x".format(mode)]) - i)

        feature = [[data["word_to_idx"][w] for w in sent] +
                   [params["VOCAB_SIZE"] + 1] *
                   (params["MAX_SENT_LEN"] - len(sent))
                   for sent in data["{}_x".format(mode)][i:i + batch_range]]
        target = [data["classes"].index(c)
                  for c in data["{}_y".format(mode)][i:i + batch_range]]

        feature = Variable(torch.LongTensor(feature))
        target = Variable(torch.LongTensor(target))
        if params["CUDA"]:
            feature = feature.cuda()
            target = target.cuda()

        logit = model(feature)
        loss = torch.nn.functional.cross_entropy(logit, target, size_average=False)
        avg_loss += loss.data[0]
        corrects += (torch.max(logit, 1)[1].view(target.size()).data == target.data).sum()


    size = len(data["{}_x".format(mode)])
    avg_loss = avg_loss / size
    accuracy = 100.0 * corrects / size

    print('{}: Evaluation - loss: {:.6f}  acc: {:.4f}%({}/{})\n'.format(mode, avg_loss, accuracy, corrects, size))

    return accuracy, avg_loss
