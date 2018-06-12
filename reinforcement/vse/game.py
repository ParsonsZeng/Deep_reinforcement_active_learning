import numpy as np
import sys
import time
import random
import torch
import torch.optim as optim
import torch.nn as nn
from torch.autograd import Variable

from config import opt, data, loaders
from data.utils import timer, average_vector, get_distance, pairwise_distances
from data.evaluation import encode_data, i2t, t2i
from data.dataset import get_active_loader


class Game:
    def reboot(self, model):
        """resets the Game Object, to make it ready for the next episode """

        loaders["active_loader"] = get_active_loader(opt.vocab)
        data_len = loaders["train_loader"].dataset.length
        self.order = random.sample(list(range(0, data_len // 5)), data_len // 5)
        self.budget = opt.budget
        self.queried_times = 0
        self.current_state = 0
        self.load_data("train")
        self.load_data("val_tot")
        self.load_data("val")
        self.init_train_k_random(model, opt.init_samples)
        timer(self.encode_episode_data, (model, "train"))
        self.performance = self.validate(model)

    def load_data(self, type):
        """ Loads data from loader[type_loader] into memory. """
        data[type] = []
        for part in loaders["{}_loader".format(type)]:
            data[type].append(part)

    def encode_episode_data(self, model, type):
        """ Encodes data from loaders["type_loader"] to use in the episode calculations """
        img_embs, cap_embs = timer(encode_data, (model, type))
        images = []

        # TODO dynamic im_div
        for i in range(0, len(img_embs), 5):
            images.append(img_embs[i].view(1, -1))
        images = torch.cat(images)

        image_caption_distances = pairwise_distances(images, cap_embs)
        topk = torch.topk(image_caption_distances, opt.topk, 1, largest=False)
        image_caption_distances_topk = topk[0]
        image_caption_distances_topk_idx = topk[1]

        data["images_embed_all"] = images.data
        data["captions_embed_all"] = cap_embs.data
        data["image_caption_distances_topk"] = image_caption_distances_topk.data
        data["image_caption_distances_topk_idx"] = image_caption_distances_topk_idx.data
        # data["img_embs_avg"] = average_vector(data["images_embed_all"])
        # data["cap_embs_avg"] = average_vector(data["captions_embed_all"])

    def get_state(self, model):
        current_idx = self.order[self.current_state]

        # Distances to topk closest captions
        state = data["image_caption_distances_topk"][current_idx].view(1, -1)
        # Softmin to make it general
        state = torch.nn.functional.softmin(state, dim=1)

        # Calculate intra-distance between closest captions
        if opt.intra_caption:
            closest_idx = data["image_caption_distances_topk_idx"][current_idx]
            closest_captions = torch.index_select(data["captions_embed_all"], 0, closest_idx)
            closest_captions_distances = pairwise_distances(closest_captions, closest_captions)
            closest_captions_intra_distance = closest_captions_distances.mean(dim=1).view(1, -1)
            state = torch.cat((state, closest_captions_intra_distance), dim=1)

        # Distances to topk closest images
        if opt.topk_image > 0:
            current_image = data["images_embed_all"][current_idx].view(1 ,-1)
            all_images = data["images_embed_all"]
            image_image_dist = pairwise_distances(current_image, all_images)
            image_image_dist_topk = torch.topk(image_image_dist, opt.topk_image, 1, largest=False)[0]

            state = torch.cat((state, image_image_dist_topk), 1)

        # Distance from average image vector
        if opt.image_distance:
            current_image = data["images_embed_all"][current_idx].view(1 ,-1)
            img_distance = get_distance(current_image, data["img_embs_avg"].view(1, -1))
            image_dist_tensor = torch.FloatTensor([img_distance]).view(1, -1)
            state = torch.cat((state, image_dist_tensor), 1)

        state = torch.autograd.Variable(state)
        if opt.cuda:
            state = state.cuda()
        self.current_state += 1
        return state

    def feedback(self, action, model):
        reward = 0.
        is_terminal = False

        if action == 1:
            timer(self.query, ())
            new_performance = self.get_performance(model)
            reward = self.performance - new_performance - opt.reward_threshold

            # TODO check batch size vs reward size
            # if opt.reward_clip:
                # reward = np.tanh(reward / 1000)

            self.performance = new_performance
        else:
            reward = 0.

        # TODO fix this
        if self.queried_times >= self.budget or self.current_state >= len(self.order):
            # Return terminal
            return None, None, True

        print("> State {:2} Action {:2} - reward {:.4f} - accuracy {:.4f}".format(
            self.current_state, action, reward, self.performance))
        next_observation = timer(self.get_state, (model,))
        return reward, next_observation, is_terminal

    def query(self):
        current = self.order[self.current_state]
        current_dist_vector = data["image_caption_distances_topk"][current].view(1, -1)
        all_dist_vectors = data["image_caption_distances_topk"]
        current_all_dist = pairwise_distances(current_dist_vector, all_dist_vectors)
        similar_indices = torch.topk(current_all_dist, opt.selection_radius, 1, largest=False)[1]

        for index in similar_indices[0]:
            image = loaders["train_loader"].dataset[5 * index][0]
            # There are 5 captions for every image
            for cap in range(5):
                caption = loaders["train_loader"].dataset[5 * index + cap][1]
                loaders["active_loader"].dataset.add_single(image, caption)
            # Only count images as an actual request.
            # Reuslt is that we have 5 times as many training points as requests.
            self.queried_times += 1

    def init_train_k_random(self, model, num_of_init_samples):
        for i in range(0, num_of_init_samples):
            current = self.order[(-1*(i + 1))]
            image = loaders["train_loader"].dataset[current][0]
            caption = loaders["train_loader"].dataset[current][1]
            loaders["active_loader"].dataset.add_single(image, caption)

        # TODO: delete used init samples (?)
        timer(self.train_model, (model, loaders["active_loader"], 30))
        print("Validation after training on random data: {}".format(self.validate(model)))

    def get_performance(self, model):
        timer(self.train_model, (model, loaders["active_loader"]))
        performance = self.validate(model)

        self.encode_episode_data(model, "train")
        return performance

    def performance_validate(self, model):
        """returns the performance messure with recall at 1, 5, 10
        for both image -> caption and cap -> img, and the sum of them all added together"""
        # compute the encoding for all the validation images and captions
        img_embs, cap_embs = encode_data(model, "val_tot")
        # caption retrieval
        (r1, r5, r10, medr, meanr) = i2t(img_embs, cap_embs, measure=opt.measure)
        # image retrieval
        (r1i, r5i, r10i, medri, meanr) = t2i(img_embs, cap_embs, measure=opt.measure)

        performance = r1 + r5 + r10 + r1i + r5i + r10i
        return (performance, r1, r5, r10, r1i, r5i, r10i)

    def validate(self, model):
        performance = timer(self.validate_loss, (model,))
        return performance

    def validate_loss(self, model):
        total_loss = 0
        model.val_start()
        for i, (images, captions, lengths, ids) in enumerate(data["val"]):
            img_emb, cap_emb = model.forward_emb(images, captions, lengths, volatile=True)
            loss = model.forward_loss(img_emb, cap_emb)
            total_loss += (loss.data.item() / opt.batch_size)
        return total_loss

    def train_model(self, model, train_loader, epochs=opt.num_epochs):
        if opt.train_shuffle:
            train_loader.dataset.shuffle()
        model.train_start()
        if len(train_loader) > 0:
            for epoch in range(epochs):
                self.adjust_learning_rate(model.optimizer, epoch)
                for i, train_data in enumerate(train_loader):
                    model.train_start()
                    model.train_emb(*train_data)

    def adjust_learning_rate(self, optimizer, epoch):
        """Sets the learning rate to the initial LR
           decayed by 10 every 30 epochs"""
        lr = opt.learning_rate_vse * (0.1 ** (epoch // opt.lr_update))
        for param_group in optimizer.param_groups:
            param_group['lr'] = lr
