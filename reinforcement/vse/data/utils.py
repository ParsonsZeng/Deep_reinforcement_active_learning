from sklearn.utils import shuffle

import pickle
import requests
import time
import torch
import plotly
import json
import copy

import numpy as np
import plotly.graph_objs as go

from datetime import datetime
from scipy import spatial
from plotly.graph_objs import Scatter, Layout
from gensim.models.keyedvectors import KeyedVectors

from data.logger import LocalLogger, ExternalLogger, NoLogger, VisdomLogger
from config import opt, data, w2v


def timer(func, args):
    """Timer function to time the duration of a spesific function func """
    time1 = time.time()
    ret = func(*args)
    time2 = time.time()
    ms = (time2 - time1) * 1000.0
    print("{}() in {:.2f} ms".format(func.__name__, ms))
    return ret


def save_model(name, model):
    print("Saving model")
    model_dict = model.state_dict()
    model_pkl = pickle.dumps(model_dict)
    url = '{}/save_model/{}'.format(opt.external_log_url, name)

    try:
        res = requests.post(url, data=model_pkl, timeout=10)
        print(res)
    except:
        print("Unable to connect to logserver. ")


def load_external_model(name):
    url = '{}/load_model/{}'.format(opt.external_log_url, name)
    res = requests.get(url)
    result = {}
    if res.ok:
        result = pickle.loads(res.content)
        print("Model loaded successfully!")
    else:
        print("###Not able to fetch model from server###")
        exit()
    return result


def external_logger():
    """function that return an logger-object to sending tensorboard logs to external server"""
    lg = ExternalLogger(opt.logger_name)
    return lg

def visdom_logger():
    lg = VisdomLogger()
    return lg


def local_logger():
    """function that return an logger-object to saving tensorboard logs locally"""
    basename = "{}logs/reinforcement/".format(opt.data_path)
    lg = LocalLogger('{}{}'.format(
        basename,
        opt.logger_name
    ))

    #need to remove the vocab object from opt because its not JSON serializable
    with open('{}{}/parameters.json'.format(basename, opt.logger_name), 'w') as outfile:
        params = {i: opt[i] for i in opt if i != 'vocab'}
        json.dump(params, outfile)
    return lg


def no_logger():
    """function that return an logger-object that will just discard everything sent to it.
    This if for testing purposes, so we don't fill up the logs with test data"""
    lg = NoLogger()
    return lg



# def save_model(model):
#     path = "saved_models/{}_{}_{}.pkl".format(opt.dataset, opt.model, opt.epoch)
#     pickle.dump(model, open(path, "wb"))
#     print("A model is saved successfully as {}!".format(path))


def load_model():
    path = "saved_models/{}_{}_{}.pkl".format(opt.dataset, opt.model, opt.epoch)

    try:
        model = pickle.load(open(path, "rb"))
        print("Model in {} loaded successfully!".format(path))

        return model
    except:
        print("No available model such as {}.".format(path))
        exit()


def logAreaGraph(distribution, classes, name):
    data = []
    for key, value in distribution.items():
        xValues = range(0, len(value))
        data.append(go.Scatter(
            name=classes[key],
            x=list(range(0, len(value))),
            y=value,
            fill='tozeroy'
        ))
    plotly.offline.plot(data, filename=name)


def load_word2vec():
    """Load word2vec pre trained vectors"""
    print("loading word2vec...")
    word_vectors = KeyedVectors.load_word2vec_format(
        "{}/GoogleNews-vectors-negative300.bin".format(opt.data_path), binary=True)

    # data["w2v_kv"] = word_vectors

    wv_matrix = []
    for word in data["vocab"]:
        if word in word_vectors.vocab:
            wv_matrix.append(word_vectors.word_vec(word))
        else:
            wv_matrix.append(
                np.random.uniform(-0.01, 0.01, 300).astype("float32"))

    # one for UNK and one for zero padding
    wv_matrix.append(np.random.uniform(-0.01, 0.01, 300).astype("float32"))
    wv_matrix.append(np.zeros(300).astype("float32"))
    wv_matrix = np.array(wv_matrix)
    w2v["w2v"] = wv_matrix
    w2v["w2v_kv"] = word_vectors
    # return word_vectors, wv_matrix


def average_vector(data):
    tot_vector = np.zeros(len(data[0]), dtype="float64") #TODO: change size to something better than data[0]
    for i in range(0,len(data)):
        tot_vector = np.add(tot_vector, torch.FloatTensor(data[i]))
    avg_vector = np.divide(tot_vector, len(data))

    return avg_vector


def get_distance(first, second):
    distance = spatial.distance.cosine(first, second)
    return distance
