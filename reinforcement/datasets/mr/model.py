import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import sklearn
import numpy as np
from pprint import pprint

from gensim.models.keyedvectors import KeyedVectors
from config import opt, data

from utils import timer, batchify, pairwise_distances


class CNN(nn.Module):
    def __init__(self):
        super(CNN, self).__init__()

        self.BATCH_SIZE = opt.batch_size
        self.MAX_SENT_LEN = 59
        self.WORD_DIM = 300
        # self.VOCAB_SIZE = 21425
        self.CLASS_SIZE = 2
        self.FILTERS = [3, 4, 5]
        self.FILTER_NUM = [100, 100, 100]
        self.DROPOUT_EMBED_PROB = 0.3
        self.DROPOUT_MODEL_PROB = 0.5
        self.IN_CHANNEL = 1
        self.VOCAB_SIZE = len(data.vocab)

        # one for UNK and one for zero padding
        # self.NUM_EMBEDDINGS = self.VOCAB_SIZE + 2
        self.NUM_EMBEDDINGS = len(data.vocab) + 2
        assert (len(self.FILTERS) == len(self.FILTER_NUM))

        if opt.cuda:
            self.cuda()

        self.reset()

    def get_conv(self, i):
        return getattr(self, 'conv_{}'.format(i))

    def reset(self):
        self.embed = nn.Embedding(self.NUM_EMBEDDINGS, self.WORD_DIM, padding_idx=self.VOCAB_SIZE + 1)

        if opt.w2v:
            print("copying w2v")
            self.embed.weight.data.copy_(torch.from_numpy(data["w2v"]))

        for i in range(len(self.FILTERS)):
            conv = nn.Conv1d(
                self.IN_CHANNEL, self.FILTER_NUM[i], self.WORD_DIM * self.FILTERS[i], stride=self.WORD_DIM)
            setattr(self, 'conv_{}'.format(i), conv)

        self.fc = nn.Linear(sum(self.FILTER_NUM), self.CLASS_SIZE)
        self.softmax = nn.LogSoftmax()
        self.dropout_embed = nn.Dropout(self.DROPOUT_EMBED_PROB)
        self.dropout = nn.Dropout(self.DROPOUT_MODEL_PROB)

        if opt.cuda:
            self.cuda()

    def forward(self, inp, include_repr=False):
        # inp = (25 x 59) - (mini_batch_size x sentence_length)
        if opt.cuda:
            inp = inp.cuda()
        x = self.embed(inp).view(-1, 1, self.WORD_DIM * self.MAX_SENT_LEN)
        x = self.dropout_embed(x)
        # x = (25 x 1 x 17700) - mini_batch_size x embedding_for_each_sentence

        conv_results = [
            F.max_pool1d(F.relu(self.get_conv(i)(x)),
                         self.MAX_SENT_LEN - self.FILTERS[i] + 1).view(-1, self.FILTER_NUM[i])
            for i in range(len(self.FILTERS))]
        conv_results = torch.cat(conv_results, 1)
        x = self.dropout(conv_results)
        x = self.fc(x)
        if include_repr:
            return x, conv_results
        else:
            return x

    def get_state(self, index):
        # state = data["all_predictions"][index]
        sentence = data["train"][0][index]
        sentence = torch.LongTensor(sentence)
        preds, repr = self.forward(sentence, include_repr=True)
        preds = nn.functional.softmax(preds, dim=1)
        state = torch.cat((repr, preds), dim=1)
        return state

    def train_model(self, train_data, epochs):
        parameters = filter(lambda p: p.requires_grad, self.parameters())
        optimizer = optim.Adadelta(parameters, 0.1)
        criterion = nn.CrossEntropyLoss()

        size = len(train_data[0])
        if size > 0:
            self.train()
            for e in range(epochs):
                train_data = sklearn.utils.shuffle(train_data[0], train_data[1])
                avg_loss = 0
                corrects = 0
                for i, (sentences, targets) in enumerate(batchify(train_data)):
                    sentences = torch.LongTensor(sentences)
                    targets = torch.LongTensor(targets)

                    if opt.cuda:
                        sentences, targets = sentences.cuda(), targets.cuda()

                    optimizer.zero_grad()
                    pred = self.forward(sentences)
                    loss = criterion(pred, targets)
                    loss.backward()
                    optimizer.step()
                    avg_loss += loss.item()
                    corrects += (torch.max(pred, 1)
                                 [1].view(targets.size()) == targets).sum()
                avg_loss = avg_loss * 32 / size

                if ((e + 1) % 10) == 0:
                    accuracy = 100.0 * corrects / size
                    s1 = "{:10s} loss: {:10.6f} acc: {:10.4f}%({}/{})".format("train", avg_loss, accuracy, corrects, size)
                    print(s1, end='\r')

    def validate(self, val_data):
        with torch.no_grad():
            self.eval()
            corrects, avg_loss = 0, 0
            for i, (sentences, targets) in enumerate(batchify(val_data)):
                sentences = torch.LongTensor(sentences)
                targets = torch.LongTensor(targets)

                if opt.cuda:
                    sentences = sentences.cuda()
                    targets = targets.cuda()

                logit = self.forward(sentences)
                loss = torch.nn.functional.cross_entropy(logit, targets, size_average=False)
                avg_loss += loss.item()
                corrects += (torch.max(logit, 1)[1].view(targets.size()) == targets).sum()

            size = len(val_data[0])
            avg_loss = avg_loss / size
            accuracy = 100.0 * corrects.double() / size

            metrics = {
                'accuracy': accuracy.item(),
                'avg_loss': avg_loss,
                'performance': accuracy.item()
            }

            return metrics

    def performance_validate(self, dataset):
        return self.validate(dataset)

    def encode_episode_data(self):
        with torch.no_grad():
            all_predictions = torch.Tensor()
            all_repr = torch.Tensor()

            if opt.cuda:
                all_predictions, all_repr = all_predictions.cuda(), all_repr.cuda()

            for i, (sentences, targets) in enumerate(batchify(data["train"])):
                # print(sentences)
                sentences = torch.LongTensor(sentences)
                targets = torch.LongTensor(targets)

                if opt.cuda:
                    sentences = sentences.cuda()

                preds, repr = self.forward(sentences, include_repr=True)
                preds = nn.functional.softmax(preds, dim=1)
                all_predictions = torch.cat((all_predictions, preds), dim=0)
                all_repr = torch.cat((all_repr, repr), dim=0)
            all_states = torch.cat((all_repr, all_predictions), dim=1)
            data["all_states"] = all_states
            print(data["all_states"].size())

    def query(self, index):
        # current_state = data["all_states"][index].view(1, -1)
        # all_states = data["all_states"]
        # current_all_dist = pairwise_distances(current_state, all_states)
        # similar_indices = torch.topk(current_all_dist, opt.selection_radius, 1, largest=False)[1]
        # similar_indices = similar_indices.data[0].cpu().numpy()

        # for idx in similar_indices:
        self.add_index(index)
        return [index]

    def add_index(self, index):
        image = data["train"][0][index]
        caption = data["train"][1][index]
        data["active"][0].append(image)
        data["active"][1].append(caption)
