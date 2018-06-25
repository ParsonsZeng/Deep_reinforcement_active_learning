import torch
import torch.nn as nn
import torch.nn.init
import torchvision.models as models
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence
import torch.backends.cudnn as cudnn
from torch.nn.utils import clip_grad_norm_
import numpy as np
import sklearn
from collections import OrderedDict
from config import opt, data
from utils import batchify, pairwise_distances, timer
from pprint import pprint
import itertools


import time
def l2norm(X):
    """L2-normalize columns of X
    """
    norm = torch.pow(X, 2).sum(dim=1, keepdim=True).sqrt()
    X = torch.div(X, norm)
    return X


def EncoderImage(data_name, img_dim, embed_size, finetune=False,
                 cnn_type='vgg19', use_abs=False, no_imgnorm=False):
    """A wrapper to image encoders. Chooses between an encoder that uses
    precomputed image features, `EncoderImagePrecomp`, or an encoder that
    computes image features on the fly `EncoderImageFull`.
    """
    if data_name.endswith('_precomp'):
        img_enc = EncoderImagePrecomp(
            img_dim, embed_size, use_abs, no_imgnorm)
    else:
        img_enc = EncoderImageFull(
            embed_size, finetune, cnn_type, use_abs, no_imgnorm)

    return img_enc


# tutorials/09 - Image Captioning
class EncoderImageFull(nn.Module):

    def __init__(self, embed_size, finetune=False, cnn_type='vgg19',
                 use_abs=False, no_imgnorm=False):
        """Load pretrained VGG19 and replace top fc layer."""
        super(EncoderImageFull, self).__init__()
        self.embed_size = embed_size
        self.no_imgnorm = no_imgnorm
        self.use_abs = use_abs

        # Load a pre-trained model
        self.cnn = self.get_cnn(cnn_type, True)

        # For efficient memory usage.
        for param in self.cnn.parameters():
            param.requires_grad = finetune

        # Replace the last fully connected layer of CNN with a new one
        if cnn_type.startswith('vgg'):
            self.fc = nn.Linear(self.cnn.classifier._modules['6'].in_features,
                                embed_size)
            self.cnn.classifier = nn.Sequential(
                *list(self.cnn.classifier.children())[:-1])
        elif cnn_type.startswith('resnet'):
            self.fc = nn.Linear(self.cnn.module.fc.in_features, embed_size)
            self.cnn.module.fc = nn.Sequential()

        self.init_weights()

    def get_cnn(self, arch, pretrained):
        """Load a pretrained CNN and parallelize over GPUs
        """
        if pretrained:
            print("=> using pre-trained model '{}'".format(arch))
            model = models.__dict__[arch](pretrained=True)
        else:
            print("=> creating model '{}'".format(arch))
            model = models.__dict__[arch]()

        if arch.startswith('alexnet') or arch.startswith('vgg'):
            model.features = nn.DataParallel(model.features)
            if opt.cuda:
                model.cuda()
        else:
            model = nn.DataParallel(model)
            if opt.cuda:
                model.cuda()

        return model

    def load_state_dict(self, state_dict):
        """
        Handle the models saved before commit pytorch/vision@989d52a
        """
        if 'cnn.classifier.1.weight' in state_dict:
            state_dict['cnn.classifier.0.weight'] = state_dict[
                'cnn.classifier.1.weight']
            del state_dict['cnn.classifier.1.weight']
            state_dict['cnn.classifier.0.bias'] = state_dict[
                'cnn.classifier.1.bias']
            del state_dict['cnn.classifier.1.bias']
            state_dict['cnn.classifier.3.weight'] = state_dict[
                'cnn.classifier.4.weight']
            del state_dict['cnn.classifier.4.weight']
            state_dict['cnn.classifier.3.bias'] = state_dict[
                'cnn.classifier.4.bias']
            del state_dict['cnn.classifier.4.bias']

        super(EncoderImageFull, self).load_state_dict(state_dict)

    def init_weights(self):
        """Xavier initialization for the fully connected layer
        """
        r = np.sqrt(6.) / np.sqrt(self.fc.in_features +
                                  self.fc.out_features)
        self.fc.weight.data.uniform_(-r, r)
        self.fc.bias.data.fill_(0)

    def forward(self, images):
        """Extract image feature vectors."""
        features = self.cnn(images)

        # normalization in the image embedding space
        features = l2norm(features)

        # linear projection to the joint embedding space
        features = self.fc(features)

        # normalization in the joint embedding space
        if not self.no_imgnorm:
            features = l2norm(features)

        # take the absolute value of the embedding (used in order embeddings)
        if self.use_abs:
            features = torch.abs(features)

        return features


class EncoderImagePrecomp(nn.Module):

    def __init__(self, img_dim, embed_size, use_abs=False, no_imgnorm=False):
        super(EncoderImagePrecomp, self).__init__()
        self.embed_size = embed_size
        self.no_imgnorm = no_imgnorm
        self.use_abs = use_abs

        self.fc = nn.Linear(img_dim, embed_size)

        self.init_weights()

    def init_weights(self):
        """Xavier initialization for the fully connected layer
        """
        r = np.sqrt(6.) / np.sqrt(self.fc.in_features +
                                  self.fc.out_features)
        self.fc.weight.data.uniform_(-r, r)
        self.fc.bias.data.fill_(0)

    def forward(self, images):
        """Extract image feature vectors."""
        # assuming that the precomputed features are already l2-normalized
        features = self.fc(images)


        # normalize in the joint embedding space
        if not self.no_imgnorm:
            features = l2norm(features)

        # take the absolute value of embedding (used in order embeddings)
        if self.use_abs:
            features = torch.abs(features)

        return features

    def load_state_dict(self, state_dict):
        """Copies parameters. overwritting the default one to
        accept state_dict from Full model
        """
        own_state = self.state_dict()
        new_state = OrderedDict()
        for name, param in state_dict.items():
            if name in own_state:
                new_state[name] = param

        super(EncoderImagePrecomp, self).load_state_dict(new_state)


# tutorials/08 - Language Model
# RNN Based Language Model
class EncoderText(nn.Module):

    def __init__(self, vocab_size, word_dim, embed_size, num_layers,
                 use_abs=False):
        super(EncoderText, self).__init__()
        self.use_abs = use_abs
        self.embed_size = embed_size

        # word embedding
        self.embed = nn.Embedding(vocab_size, word_dim)

        # caption embedding
        self.rnn = nn.GRU(word_dim, embed_size, num_layers, batch_first=True)

        self.init_weights()

    def init_weights(self):
        self.embed.weight.data.uniform_(-0.1, 0.1)
        if opt.w2v:
            print("copying w2v")
            self.embed.weight.data.copy_(torch.from_numpy(data.w2v))

    def forward(self, x, lengths):
        """Handles variable size captions
        """
        # Embed word ids to vectors
        x = self.embed(x)
        packed = pack_padded_sequence(x, lengths, batch_first=True)

        # Forward propagate RNN
        out, _ = self.rnn(packed)

        # Reshape *final* output to (batch_size, hidden_size)
        padded = pad_packed_sequence(out, batch_first=True)
        I = torch.LongTensor(lengths).view(-1, 1, 1)
        I = I.expand(x.size(0), 1, self.embed_size)-1
        if opt.cuda:
            I = I.cuda()
        out = torch.gather(padded[0], 1, I).squeeze(1)

        # normalization in the joint embedding space
        out = l2norm(out)

        # take absolute value, used by order embeddings
        if self.use_abs:
            out = torch.abs(out)

        return out


def cosine_sim(im, s):
    """Cosine similarity between all the image and sentence pairs
    """
    return im.mm(s.t())


def order_sim(im, s):
    """Order embeddings similarity measure $max(0, s-im)$
    """
    YmX = (s.unsqueeze(1).expand(s.size(0), im.size(0), s.size(1))
           - im.unsqueeze(0).expand(s.size(0), im.size(0), s.size(1)))
    score = -YmX.clamp(min=0).pow(2).sum(2).sqrt().t()
    return score


class ContrastiveLoss(nn.Module):
    """
    Compute contrastive loss
    """

    def __init__(self, margin=0, measure=False, max_violation=False):
        super(ContrastiveLoss, self).__init__()
        self.margin = margin
        if measure == 'order':
            self.sim = order_sim
        else:
            self.sim = cosine_sim

        self.max_violation = max_violation

    def forward(self, im, s):
        # compute image-sentence score matrix
        scores = self.sim(im, s)
        diagonal = scores.diag().view(im.size(0), 1)
        d1 = diagonal.expand_as(scores)
        d2 = diagonal.t().expand_as(scores)
        # print(scores)
        # print(d1)
        # compare every diagonal score to scores in its column
        # caption retrieval
        cost_s = (self.margin + scores - d1).clamp(min=0)
        # compare every diagonal score to scores in its row
        # image retrieval
        cost_im = (self.margin + scores - d2).clamp(min=0)
        # clear diagonals
        mask = torch.eye(scores.size(0)) > .5
        I = mask
        if opt.cuda:
            I = I.cuda()
        cost_s = cost_s.masked_fill_(I, 0)
        cost_im = cost_im.masked_fill_(I, 0)

        # keep the maximum violating negative for each query
        if self.max_violation:
            cost_s = cost_s.max(1)[0]
            cost_im = cost_im.max(0)[0]

        return cost_s.sum() + cost_im.sum()


class VSE(nn.Module):
    """
    rkiros/uvs model
    """

    def __init__(self):
        super(VSE, self).__init__()
        # tutorials/09 - Image Captioning
        # Build Models
        self.grad_clip = opt.grad_clip
        self.img_enc = EncoderImage(opt.data_name, opt.img_dim, opt.embed_size,
                                    opt.finetune, opt.cnn_type,
                                    use_abs=opt.use_abs,
                                    no_imgnorm=opt.no_imgnorm)
        self.txt_enc = EncoderText(opt.vocab_size, opt.word_dim,
                                   opt.embed_size, opt.num_layers,
                                   use_abs=opt.use_abs)
        if opt.cuda:
            self.img_enc.cuda()
            self.txt_enc.cuda()
            # cudnn.benchmark = True

        # Loss and Optimizer
        self.criterion = ContrastiveLoss(margin=opt.margin,
                                         measure=opt.measure,
                                         max_violation=opt.max_violation)
        params = list(self.txt_enc.parameters())
        params += list(self.img_enc.fc.parameters())
        if opt.finetune:
            params += list(self.img_enc.cnn.parameters())
        self.params = params

        self.optimizer = torch.optim.Adam(params, lr=opt.learning_rate_vse)

    def reset(self):
        self.img_enc = EncoderImage(opt.data_name, opt.img_dim, opt.embed_size,
                                    opt.finetune, opt.cnn_type,
                                    use_abs=opt.use_abs,
                                    no_imgnorm=opt.no_imgnorm)
        self.txt_enc = EncoderText(opt.vocab_size, opt.word_dim,
                                   opt.embed_size, opt.num_layers,
                                   use_abs=opt.use_abs)
        if opt.cuda:
            self.img_enc.cuda()
            self.txt_enc.cuda()

        params = list(self.txt_enc.parameters())
        params += list(self.img_enc.fc.parameters())
        if opt.finetune:
            params += list(self.img_enc.cnn.parameters())
        self.params = params
        self.optimizer = torch.optim.Adam(params, lr=opt.learning_rate_vse)


    def state_dict(self):
        state_dict = [self.img_enc.state_dict(), self.txt_enc.state_dict()]
        return state_dict

    def load_state_dict(self, state_dict):
        self.img_enc.load_state_dict(state_dict[0])
        self.txt_enc.load_state_dict(state_dict[1])

    def train_start(self):
        """switch to train mode
        """
        self.img_enc.train()
        self.txt_enc.train()
        # self.train()

    def val_start(self):
        """switch to evaluate mode
        """
        # self.eval()
        self.img_enc.eval()
        self.txt_enc.eval()

    def forward_img(self, images, volatile=True):
        if volatile:
            torch.set_grad_enabled(False)
        if opt.cuda:
            images = images.cuda()
        img_emb = self.img_enc(images)
        torch.set_grad_enabled(True)
        return img_emb

    def forward_cap(self, captions, lengths, volatile=True):
        torch.set_grad_enabled(not volatile)
        if opt.cuda:
            captions = captions.cuda()
        cap_emb = self.txt_enc(captions, lengths)
        torch.set_grad_enabled(True)
        return cap_emb

    def forward_emb(self, images, captions, lengths, volatile=False):
        """Compute the image and caption embeddings
        """
        # Set mini-batch dataset
        torch.set_grad_enabled(not volatile)
        images = torch.FloatTensor(images)
        captions = torch.LongTensor(captions)
        if opt.cuda:
            images = images.cuda()
            captions = captions.cuda()

        # if volatile:
        #     with torch.no_grad():
        #         # Forward
        #         img_emb = self.img_enc(images)
        #         cap_emb = self.txt_enc(captions, lengths)
        # else:
        img_emb = self.img_enc(images)
        cap_emb = self.txt_enc(captions, lengths)
        del images, captions
        torch.set_grad_enabled(True)
        return img_emb, cap_emb

    def forward_loss(self, img_emb, cap_emb, **kwargs):
        """Compute the loss given pairs of image and caption embeddings
        """
        loss = self.criterion(img_emb, cap_emb)
        # self.logger.update('Le', loss.data[0], img_emb.size(0))
        return loss

    def train_emb(self, images, captions, lengths, ids=None, *args):
        """One training step given images and captions.
        """
        # compute the embeddings
        img_emb, cap_emb = self.forward_emb(images, captions, lengths)

        # measure accuracy and record loss
        self.optimizer.zero_grad()

        loss = self.forward_loss(img_emb, cap_emb)

        # compute gradient and do SGD step
        loss.backward()
        if self.grad_clip > 0:
            clip_grad_norm_(self.params, self.grad_clip)
        self.optimizer.step()
        del img_emb, cap_emb
        return loss

    def query(self, index):
        # current_vector = torch.Tensor(data["train_deleted"][0][index]).view(1, -1)
        # all_vectors = torch.Tensor(data["train_deleted"][0])
        # current_vector = all_vectors[index].view(1, -1)
        # if opt.cuda:
            # all_vectors, current_vector = all_vectors.cuda(), current_vector.cuda()

        current_state = data["all_states"][index].view(1, -1)
        all_states = data["all_states"]
        current_all_dist = pairwise_distances(current_state, all_states)
        similar_indices = torch.topk(current_all_dist, opt.selection_radius * 5, 1, largest=False)[1]
        similar_indices = similar_indices.data[0].cpu().numpy()
        for idx in similar_indices:
            self.add_index(idx)
        return similar_indices

    def add_index(self, index):
        image = data["train_deleted"][0][index]
        caption = data["train_deleted"][1][index]
        length = data["train_deleted"][2][index]
        data["active"][0].append(image)
        data["active"][1].append(caption)
        data["active"][2].append(length)

    def encode_data(self, dataset):
        """Encode all images and captions loadable by `data_loader`
        """
        # with torch.no_grad():
        torch.set_grad_enabled(False)
        self.val_start()
        img_embs = []
        cap_embs = []
        for i, (images, captions, lengths) in enumerate(batchify(dataset)):
            # compute the embeddings
            img_emb, cap_emb = self.forward_emb(images, captions, lengths, volatile=True)
            img_embs.append(img_emb)
            cap_embs.append(cap_emb)
            del img_emb, cap_emb
            del images, captions
        img_embs = torch.cat(img_embs)
        cap_embs = torch.cat(cap_embs)
        torch.set_grad_enabled(True)
        return img_embs, cap_embs

    def train_model(self, train_data, epochs):
        # if opt.train_shuffle:
            # train_data = sklearn.utils.shuffle(*train_data)

        # Sort the data by descending lengths
        sort_idx = np.argsort(-1 * np.array(train_data[2]))
        train_cap_lengths = np.array(train_data[2])[sort_idx]
        train_images = np.array(train_data[0])[sort_idx]
        train_tokens = np.array(train_data[1])[sort_idx]
        train_data = (train_images, train_tokens, train_cap_lengths)
        self.train_start()

        if len(train_data[0]) > 0:
            for epoch in range(epochs):
                self.adjust_learning_rate(self.optimizer, epoch)
                for i, minibatch in enumerate(batchify(train_data)):
                    if(len(minibatch[2]) > 0):
                        self.train_start()
                        self.train_emb(*minibatch)
                        del minibatch
        return 13

    def validate(self, dataset):
        # total_loss = 0
        # self.val_start()
        # # for i, (images, captions, lengths, ids) in enumerate(loader):
        # for i, (images, captions, lengths) in enumerate(batchify(dataset)):
        #     img_emb, cap_emb = self.forward_emb(images, captions, lengths, volatile=True)
        #     loss = self.forward_loss(img_emb, cap_emb)
        #     total_loss += loss.data.item()
        # total_loss = total_loss / len(dataset[0])
        #
        # metrics = {
        #     "performance": -1 * total_loss
        # }
        # return metrics
        return self.performance_validate(dataset)

    def performance_validate(self, dataset):
        """returns the performance messure with recall at 1, 5, 10
        for both image -> caption and cap -> img, and the sum of them all added together"""

        img_embs, cap_embs = self.encode_data(dataset)
        (r1, r5, r10, r1i, r5i, r10i) = t2i2t(img_embs, cap_embs)

        sum = r1 + r5 + r10 + r1i + r5i + r10i
        performance = r1 + r5 + r10
        metrics = {
            "performance": performance,
            "sum": sum,
            "r1": r1,
            "r5": r5,
            "r10": r10,
            "r1i": r1i,
            "r5i": r5i,
            "r10i": r10i
        }

        return metrics

    def encode_episode_data(self):
        # pass
        """ Encodes data from data["train"] to use in the episode calculations """
        #
        torch.set_grad_enabled(False)
        dataset = data["train_deleted"]
        img_embs, cap_embs = timer(self.encode_data, (dataset,))
        if opt.cuda:
            img_embs = img_embs.cuda()
            cap_embs = cap_embs.cuda()
        image_caption_distances = timer(pairwise_distances, (img_embs, cap_embs))
        topk = torch.topk(image_caption_distances, opt.topk, 1, largest=False)
        (image_caption_distances_topk, image_caption_distances_topk_idx) = (topk[0], topk[1])
        data["image_caption_distances_topk"] = image_caption_distances_topk
        data["image_caption_distances_topk_idx"] = image_caption_distances_topk_idx
        del topk
        del image_caption_distances
        intra_cap_distance = timer(pairwise_distances, (cap_embs, cap_embs))
        select_indices_row = []
        select_indices_col = []

        for row in data["image_caption_distances_topk_idx"].cpu().numpy():
            permutations = list(zip(*itertools.permutations(row, 2)))
            permutations_list = [list(p) for p in permutations]
            select_indices_row.extend(permutations_list[0])
            select_indices_col.extend(permutations_list[1])

        all_dist = intra_cap_distance[select_indices_row, select_indices_col]
        all_dist = all_dist.view(len(data["train_deleted"][0]), opt.topk, opt.topk -1)
        all_dist = all_dist.mean(dim=2)
        # all_img = torch.Tensor(data["train_deleted"][0])
        # print(all_img.size())
        # print(data["image_caption_distances_topk"].size())
        # print(all_dist.size())
        # data["all_states"] = torch.cat((all_img, data["image_caption_distances_topk"].cpu(), all_dist.cpu()), 1)
        # print(data["all_states"].size())
        print(data["image_caption_distances_topk"].size())
        data["all_states"] = torch.cat((all_dist, data["image_caption_distances_topk"]), dim=1).cpu()
        print(data["all_states"].size())
        # data["images_embed_all"] = img_embs.data.cpu()
        # data["captions_embed_all"] = cap_embs.data.cpu()
        # all_dist = all_dist.cpu()
        # data["all_states"] = all_dist.cpu()
        # print(data["all_states"].size())

        # Testing for fixed index to see if it works
        # test_idx = 1337
        # top_cap_idx = data["image_caption_distances_topk_idx"][test_idx]
        # top_cap = cap_embs.index_select(0, top_cap_idx)
        #
        # top_cap_intra_dist = pairwise_distances(top_cap, top_cap)
        # # print(top_cap_intra_dist)
        # top_cap_intra_dist = top_cap_intra_dist[top_cap_intra_dist > 0.0001].view(opt.topk, -1)
        # top_cap_mean_intra_dist = top_cap_intra_dist.mean(dim=1)
        # print(top_cap_mean_intra_dist)
        # print(data["all_states"][test_idx])

        del intra_cap_distance
        del img_embs
        del cap_embs
        torch.set_grad_enabled(True)

    def get_state(self, index):
        with torch.no_grad():
            # state = torch.Tensor(data["train_deleted"][0][index]
            state = data["all_states"][index].view(1, -1)
            if opt.cuda:
                state = state.cuda()
            return state
        # index = index * 5
        # with torch.no_grad():
        #     # Distances to topk closest captions
        #     state = data["image_caption_distances_topk"][index].view(1, -1)
        #     # The distances themselves are very small. Scale them to increase the
        #     # differences
        #     state = state * 15
        #     # Softmin to make it general
        #     state = torch.nn.functional.softmin(state, dim=1)
        #
        #     # Calculate intra-distance between closest captions
        #     if opt.intra_caption:
        #         closest_idx = data["image_caption_distances_topk_idx"][index]
        #         closest_captions = torch.index_select(data["captions_embed_all"], 0, closest_idx)
        #         closest_captions_distances = pairwise_distances(closest_captions, closest_captions)
        #         closest_captions_intra_distance = closest_captions_distances.mean(dim=1).view(1, -1)
        #         state = torch.cat((state, closest_captions_intra_distance), dim=1)
        #
        #     # Distances to topk closest images
        #     if opt.topk_image > 0:
        #         current_image = data["images_embed_all"][index].view(1 ,-1)
        #         all_images = data["images_embed_all"]
        #         image_image_dist = pairwise_distances(current_image, all_images)
        #         image_image_dist_topk = torch.topk(image_image_dist, opt.topk_image, 1, largest=False)[0]
        #
        #         state = torch.cat((state, image_image_dist_topk), 1)
        #
        #     # Distance from average image vector
        #     if opt.image_distance:
        #         current_image = data["images_embed_all"][index].view(1 ,-1)
        #         img_distance = get_distance(current_image, data["img_embs_avg"].view(1, -1))
        #         image_dist_tensor = torch.FloatTensor([img_distance]).view(1, -1)
        #         state = torch.cat((state, image_dist_tensor), 1)
        #
        #     state = torch.autograd.Variable(state)
        #     if opt.cuda:
        #         state = state.cuda()
        #     return state
        # print(data["all_states"].size())
        # state = data["all_states"][index].view(1, -1)
        # print(state)
        # print(state.size())
            # if opt.cuda:
            #     state = state.cuda()
            # return state


    def adjust_learning_rate(self, optimizer, epoch):
        """Sets the learning rate to the initial LR
           decayed by 10 every 30 epochs"""
        lr = opt.learning_rate_vse * (0.1 ** (epoch // opt.lr_update))
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr


def i2t(images, captions, npts=None, measure='cosine', return_ranks=False):
    """
    Images->Text (Image Annotation)
    Images: (5N, K) matrix of images
    Captions: (5N, K) matrix of captions
    """
    images = images.cpu().numpy()
    captions = captions.cpu().numpy()
    if npts is None:
        npts = images.shape[0] / 5
    index_list = []

    # TODO check if this is always correct
    npts = int(npts)
    ranks = np.zeros(npts)
    top1 = np.zeros(npts)
    for index in range(npts):

        # Get query image
        im = images[5 * index].reshape(1, images.shape[1])

        # Compute scores
        if measure == 'order':
            bs = 100
            if index % bs == 0:
                mx = min(images.shape[0], 5 * (index + bs))
                im2 = images[5 * index:mx:5]
                a = torch.Tensor(im2)
                b = torch.Tensor(captions)
                if opt.cuda:
                    a, b = a.cuda(), b.cuda()
                d2 = order_sim(a,b)
                d2 = d2.cpu().numpy()
            d = d2[index % bs]
        else:
            d = np.dot(im, captions.T).flatten()
        inds = np.argsort(d)[::-1]
        index_list.append(inds[0])

        # Score
        rank = 1e20
        for i in range(5 * index, 5 * index + 5, 1):
            tmp = np.where(inds == i)[0][0]
            if tmp < rank:
                rank = tmp
        ranks[index] = rank
        top1[index] = inds[0]

    # Compute metrics
    r1 = 100.0 * len(np.where(ranks < 1)[0]) / len(ranks)
    r5 = 100.0 * len(np.where(ranks < 5)[0]) / len(ranks)
    r10 = 100.0 * len(np.where(ranks < 10)[0]) / len(ranks)
    medr = np.floor(np.median(ranks)) + 1
    meanr = ranks.mean() + 1
    if return_ranks:
        return (r1, r5, r10, medr, meanr), (ranks, top1)
    else:
        return (r1, r5, r10, medr, meanr)


def t2i2t(images, captions):
    image_caption_distances = pairwise_distances(images, captions)
    topk_idx = torch.topk(image_caption_distances, 10 , 1, largest=False)[1]
    ranks = []
    for i, row in enumerate(topk_idx):
        rank = np.where(row.cpu().numpy() == i)
        ranks.append(rank)

    ranks = np.array(ranks)
    r1 = 100.0 * len(np.where(ranks < 1)[0]) / len(ranks)
    r5 = 100.0 * len(np.where(ranks < 5)[0]) / len(ranks)
    r10 = 100.0 * len(np.where(ranks < 10)[0]) / len(ranks)

    image_caption_distances = image_caption_distances.t()
    topk_idx = torch.topk(image_caption_distances, 10 , 1, largest=False)[1]
    ranks = []
    for i, row in enumerate(topk_idx):
        rank = np.where(row.cpu().numpy() == i)
        ranks.append(rank)

    ranks = np.array(ranks)
    r1i = 100.0 * len(np.where(ranks < 1)[0]) / len(ranks)
    r5i = 100.0 * len(np.where(ranks < 5)[0]) / len(ranks)
    r10i = 100.0 * len(np.where(ranks < 10)[0]) / len(ranks)
    # print((r1, r5, r10, r1i, r5i, r10i))
    return (r1, r5, r10, r1i, r5i, r10i)
