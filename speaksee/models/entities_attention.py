from __future__ import division
from __future__ import absolute_import
import torch
from torch import nn
from torch import distributions
import torch.nn.functional as F
import numpy as np
from .CaptioningModel import CaptioningModel


def update_loop(b_s, regions, device, t, seq_len, det_ids):
    # id_curr = torch.argmax(det_ids[:, t:] != 0, -1)

    det_ids_cpu = det_ids.data.cpu().numpy()
    cond = np.zeros((b_s, seq_len))
    cond[:, t:] = det_ids_cpu[:, t:] != 0
    id_curr_cpu = np.expand_dims(np.argmax(cond, -1), -1)
    id_curr = torch.from_numpy(id_curr_cpu).to(device)  # (b_s, 1)
    det_ids_curr = torch.gather(det_ids, 1, id_curr)  # (b_s, 1)
    det_ids_curr_exp = det_ids_curr.unsqueeze(-1).expand((b_s, 1, regions.shape[-1]))  # (b_s, 1, d)
    det_curr = torch.gather(regions, 1, det_ids_curr_exp)

    cond1 = det_ids_cpu[:, t+2:] != id_curr_cpu
    cond2 = cond1 * (det_ids_cpu[:, t+2:] != 0)
    cond = np.zeros((b_s, seq_len))
    cond[:, t+2:] = cond2
    id_next = torch.from_numpy(np.argmax(cond, -1)).unsqueeze(-1).to(device)  # (b_s, 1)
    det_ids_next = torch.gather(det_ids, 1, id_next)  # (b_s, 1)
    det_ids_next_exp = det_ids_next.unsqueeze(-1).expand((b_s, 1, regions.shape[-1]))  # (b_s, 1, d)
    det_next = torch.gather(regions, 1, det_ids_next_exp)

    return det_curr, det_next


def update_loop_coco(b_s, regions, device, t, seq_len, all_det_ids):
    # id_curr = torch.argmax(det_ids[:, t:] != 0, -1)

    det_ids = all_det_ids[:, :, 0]
    det_ids_cpu = det_ids.data.cpu().numpy()
    cond = np.zeros((b_s, seq_len))
    cond[:, t:] = det_ids_cpu[:, t:] != 0
    id_curr_cpu = np.expand_dims(np.argmax(cond, -1), -1)
    id_curr = torch.from_numpy(id_curr_cpu).to(device)  # (b_s, 1)
    det_ids_curr = torch.gather(all_det_ids, 1, id_curr.unsqueeze(-1).expand((-1, -1, all_det_ids.shape[-1])))[:,:,1:]  # (b_s, 1, 100), exclude first detection
    det_ids_curr_mask = det_ids_curr != -1
    det_ids_curr[det_ids_curr_mask == 0] = 0
    det_ids_curr_exp = det_ids_curr.squeeze(1).unsqueeze(-1).expand((-1, -1, regions.shape[-1]))  # (b_s, 100, d)
    det_curr = torch.gather(regions, 1, det_ids_curr_exp) # (b_s, 100, d)

    cond1 = det_ids_cpu[:, t+2:] != id_curr_cpu
    cond2 = cond1 * (det_ids_cpu[:, t+2:] != 0)
    cond = np.zeros((b_s, seq_len))
    cond[:, t+2:] = cond2
    id_next = torch.from_numpy(np.argmax(cond, -1)).unsqueeze(-1).to(device)  # (b_s, 1)
    det_ids_next = torch.gather(det_ids, 1, id_next)  # (b_s, 1)
    det_ids_next_exp = det_ids_next.unsqueeze(-1).expand((b_s, 1, regions.shape[-1]))  # (b_s, 1, d)
    det_next = torch.gather(regions, 1, det_ids_next_exp)

    return det_curr, det_ids_curr_mask.squeeze(1), det_next


def update_loop_with_lstm(b_s, regions, device, t, seq_len, det_ids):
    det_ids_cpu = det_ids.data.cpu().numpy()
    cond = np.zeros((b_s, seq_len))
    cond[:, t:] = det_ids_cpu[:, t:] != 0
    id_curr_cpu = np.expand_dims(np.argmax(cond, -1), -1)
    id_curr = torch.from_numpy(id_curr_cpu).to(device)  # (b_s, 1)
    det_ids_curr = torch.gather(det_ids, 1, id_curr)  # (b_s, 1)
    det_ids_curr_exp = det_ids_curr.unsqueeze(-1).expand((b_s, 1, regions.shape[-1]))  # (b_s, 1, d)
    det_curr = torch.gather(regions, 1, det_ids_curr_exp)

    det_next_ids = []
    det_next_regions = []
    det_ids_next = det_ids[:, t:]
    det_ids_next = torch.cat([det_ids_next[:, :1], det_ids_next], 1)
    for i in range(det_ids_next.shape[0]):
        row = det_ids_next[i]
        valid_idxs = ((row[1:] != 0) * (row[1:] != row[:-1])).nonzero()
        if valid_idxs.shape[0] < 2:
            valid = torch.zeros(1).long().to(device)
        else:
            valid_idxs = valid_idxs[:, 0]
            valid = torch.index_select(row[1:], 0, valid_idxs[1:])
        valid_regions = torch.index_select(regions[i], 0, valid)
        det_next_ids.append(valid)
        det_next_regions.append(valid_regions)

    return det_curr, (det_next_ids, det_next_regions)


class EntitiesAttention(CaptioningModel):
    def __init__(self, vocab_size, det_feat_size=2048, input_encoding_size=1000, rnn_size=1000, att_size=512, ss_prob=.0):
        super(EntitiesAttention, self).__init__()
        self.vocab_size = vocab_size
        self.det_feat_size = det_feat_size
        self.input_encoding_size = input_encoding_size
        self.rnn_size = rnn_size
        self.att_size = att_size

        self.embed = nn.Embedding(vocab_size, input_encoding_size)

        self.W1_is = nn.Linear(det_feat_size + rnn_size + input_encoding_size, rnn_size)
        self.W1_hs = nn.Linear(rnn_size, rnn_size)

        self.lstm_cell_1 = nn.LSTMCell(det_feat_size + rnn_size + input_encoding_size, rnn_size)
        self.lstm_cell_2 = nn.LSTMCell(rnn_size + det_feat_size + det_feat_size, rnn_size)

        self.out_fc = nn.Linear(rnn_size, vocab_size)
        self.s_fc = nn.Linear(rnn_size, det_feat_size)

        self.ss_prob = ss_prob
        self.init_weights()

    def init_weights(self):
        nn.init.xavier_normal_(self.embed.weight)
        nn.init.xavier_normal_(self.out_fc.weight)
        nn.init.constant_(self.out_fc.bias, 0)

        nn.init.xavier_normal_(self.s_fc.weight)
        nn.init.constant_(self.s_fc.bias, 0)

        nn.init.xavier_normal_(self.lstm_cell_1.weight_ih)
        nn.init.orthogonal_(self.lstm_cell_1.weight_hh)
        nn.init.constant_(self.lstm_cell_1.bias_ih, 0)
        nn.init.constant_(self.lstm_cell_1.bias_hh, 0)

        nn.init.xavier_normal_(self.lstm_cell_2.weight_ih)
        nn.init.orthogonal_(self.lstm_cell_2.weight_hh)
        nn.init.constant_(self.lstm_cell_2.bias_ih, 0)
        nn.init.constant_(self.lstm_cell_2.bias_hh, 0)

    def init_state(self, b_s, device):
        h0_1 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        c0_1 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        h0_2 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        c0_2 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        return (h0_1, c0_1), (h0_2, c0_2)

    def forward(self, detections, seq, det_ids):
        device = detections.device
        b_s = detections.size(0)
        seq_len = seq.size(1)
        detections_mask = (torch.sum(detections, -1, keepdim=True) != 0).float()
        detections_mean = torch.sum(detections, 1) / torch.sum(detections_mask, 1)

        state_1, state_2 = self.init_state(b_s, device)
        outputs = []

        for t in range(seq_len-1):
            if self.training and t > 0 and self.ss_prob > .0:
                # Scheduled sampling
                coin = detections.data.new(b_s).uniform_(0, 1)
                coin = (coin < self.ss_prob).long()
                distr = distributions.Categorical(logits=outputs[-1].squeeze(1))
                action = distr.sample()
                it = coin * action.data + (1-coin) * seq[:, t-1].data
                it = it.to(device)
            else:
                it = seq[:, t]
            xt = self.embed(it)

            input_1 = torch.cat([state_2[0], detections_mean, xt], 1)
            s_gate = torch.sigmoid(self.W1_is(input_1) + self.W1_hs(state_1[0]))
            state_1 = self.lstm_cell_1(input_1, state_1)

            s_t = s_gate * torch.tanh(state_1[1])
            s_t = F.relu(self.s_fc(s_t))

            regions = torch.cat([s_t.unsqueeze(1), detections], 1)
            indexes = det_ids[:, t+1]
            current_regions = torch.gather(regions, 1, indexes.unsqueeze(-1).unsqueeze(-1).expand(b_s, 1, regions.size(-1)))

            next_regions = torch.zeros(b_s, 1, regions.size(-1)).to(device)
            for i in range(b_s):
                next_regions[i] = regions[i][0]
                for j in range(t+2, seq_len-1):
                    if det_ids[i][j] != 0:
                        next_regions[i] = regions[i][j]
                        break

            input_2 = torch.cat([state_1[0], current_regions.squeeze(1), next_regions.squeeze(1)], 1)

            state_2 = self.lstm_cell_2(input_2, state_2)
            out = F.log_softmax(self.out_fc(state_2[0]), dim=-1)
            outputs.append(out.unsqueeze(1))

        return torch.cat(outputs, 1)

    def test(self, detections, det_ids, seq_len, bos_idx):
        device = detections.device
        b_s = detections.size(0)
        detections_mask = (torch.sum(detections, -1, keepdim=True) != 0).float()
        detections_mean = torch.sum(detections, 1) / torch.sum(detections_mask, 1)

        det_ids_pad = torch.zeros(b_s, seq_len).long().to(device)
        if det_ids.size(1) < seq_len - 1:
            det_ids_pad[:, :det_ids.size(1)] = det_ids
        else:
            det_ids_pad = det_ids[:, :seq_len]

        state_1, state_2 = self.init_state(b_s, device)
        outputs = []

        for t in range(seq_len-1):
            if t == 0:
                it = detections.data.new_full((b_s,), bos_idx).long()
            else:
                it = torch.max(outputs[-1].squeeze(1), -1)[1]
            xt = self.embed(it)

            input_1 = torch.cat([state_2[0], detections_mean, xt], 1)
            s_gate = torch.sigmoid(self.W1_is(input_1) + self.W1_hs(state_1[0]))
            state_1 = self.lstm_cell_1(input_1, state_1)

            s_t = s_gate * torch.tanh(state_1[1])
            s_t = F.relu(self.s_fc(s_t))

            regions = torch.cat([s_t.unsqueeze(1), detections], 1)
            indexes = det_ids_pad[:, t + 1]
            current_regions = torch.gather(regions, 1, indexes.unsqueeze(-1).unsqueeze(-1).expand(b_s, 1, regions.size(-1)))

            next_regions = torch.zeros(b_s, 1, regions.size(-1)).to(device)
            for i in range(b_s):
                next_regions[i] = regions[i][0]
                for j in range(t + 2, seq_len - 1):
                    if det_ids_pad[i][j] != 0:
                        next_regions[i] = regions[i][j]
                        break

            input_2 = torch.cat([state_1[0], current_regions.squeeze(1), next_regions.squeeze(1)], 1)

            state_2 = self.lstm_cell_2(input_2, state_2)
            out = F.log_softmax(self.out_fc(state_2[0]), dim=-1)
            outputs.append(out.unsqueeze(1))

        return torch.max(torch.cat(outputs, 1), -1)[1]


class EntitiesAttentionImproved(CaptioningModel):
    def __init__(self, vocab_size, det_feat_size=2048, input_encoding_size=1000, rnn_size=1000, att_size=512, ss_prob=.0,
                 h2_fist_lstm=True, det_next_second_lstm=True, img_second_lstm=False):
        super(EntitiesAttentionImproved, self).__init__()
        self.vocab_size = vocab_size
        self.det_feat_size = det_feat_size
        self.input_encoding_size = input_encoding_size
        self.rnn_size = rnn_size
        self.att_size = att_size
        self.h2_fist_lstm = h2_fist_lstm
        self.det_next_second_lstm = det_next_second_lstm
        self.img_second_lstm = img_second_lstm

        self.embed = nn.Embedding(vocab_size, input_encoding_size)

        if self.h2_fist_lstm:
            self.W1_is = nn.Linear(det_feat_size + rnn_size + input_encoding_size, rnn_size)
        else:
            self.W1_is = nn.Linear(det_feat_size + input_encoding_size, rnn_size)
        self.W1_hs = nn.Linear(rnn_size, rnn_size)

        self.att_va = nn.Linear(det_feat_size, att_size, bias=False)
        self.att_ha = nn.Linear(rnn_size, att_size, bias=False)
        self.att_a = nn.Linear(att_size, 1, bias=False)

        self.att_s = nn.Linear(det_feat_size, att_size, bias=False)

        if self.h2_fist_lstm:
            self.lstm_cell_1 = nn.LSTMCell(det_feat_size + rnn_size + input_encoding_size, rnn_size)
        else:
            self.lstm_cell_1 = nn.LSTMCell(det_feat_size + input_encoding_size, rnn_size)

        if self.det_next_second_lstm and self.img_second_lstm:
            self.lstm_cell_2 = nn.LSTMCell(rnn_size + det_feat_size + det_feat_size + det_feat_size, rnn_size)
        elif self.det_next_second_lstm ^ self.img_second_lstm:
            self.lstm_cell_2 = nn.LSTMCell(rnn_size + det_feat_size + det_feat_size, rnn_size)

        self.out_fc = nn.Linear(rnn_size, vocab_size)
        self.s_fc = nn.Linear(rnn_size, det_feat_size)

        self.ss_prob = ss_prob
        self.init_weights()

    def init_weights(self):
        nn.init.xavier_normal_(self.embed.weight)
        nn.init.xavier_normal_(self.out_fc.weight)
        nn.init.constant_(self.out_fc.bias, 0)

        nn.init.xavier_normal_(self.s_fc.weight)
        nn.init.constant_(self.s_fc.bias, 0)

        nn.init.xavier_normal_(self.att_va.weight)
        nn.init.xavier_normal_(self.att_ha.weight)
        nn.init.xavier_normal_(self.att_a.weight)
        nn.init.xavier_normal_(self.att_s.weight)

        nn.init.xavier_normal_(self.lstm_cell_1.weight_ih)
        nn.init.orthogonal_(self.lstm_cell_1.weight_hh)
        nn.init.constant_(self.lstm_cell_1.bias_ih, 0)
        nn.init.constant_(self.lstm_cell_1.bias_hh, 0)

        nn.init.xavier_normal_(self.lstm_cell_2.weight_ih)
        nn.init.orthogonal_(self.lstm_cell_2.weight_hh)
        nn.init.constant_(self.lstm_cell_2.bias_ih, 0)
        nn.init.constant_(self.lstm_cell_2.bias_hh, 0)

    def init_state(self, b_s, device):
        h0_1 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        c0_1 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        h0_2 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        c0_2 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        return (h0_1, c0_1), (h0_2, c0_2)

    def forward(self, detections, images, seq, det_ids):
        device = detections.device
        b_s = detections.size(0)
        seq_len = seq.size(1)
        if images is not None:
            image_descriptor = images
        else:
            detections_mask = (torch.sum(detections, -1, keepdim=True) != 0).float()
            image_descriptor = torch.sum(detections, 1) / torch.sum(detections_mask, 1)

        state_1, state_2 = self.init_state(b_s, device)
        outputs = []
        att_outputs = []

        for t in range(seq_len-1):
            # if self.training and t > 0 and self.ss_prob > .0:
            #     # Scheduled sampling
            #     coin = detections.data.new(b_s).uniform_(0, 1)
            #     coin = (coin < self.ss_prob).long()
            #     distr = distributions.Categorical(logits=outputs[-1].squeeze(1))
            #     action = distr.sample()
            #     it = coin * action.data + (1-coin) * seq[:, t-1].data
            #     it = it.to(device)
            # else:
            it = seq[:, t]
            xt = self.embed(it)

            if self.h2_fist_lstm:
                input_1 = torch.cat([state_2[0], image_descriptor, xt], 1)
            else:
                input_1 = torch.cat([image_descriptor, xt], 1)

            s_gate = torch.sigmoid(self.W1_is(input_1) + self.W1_hs(state_1[0]))
            state_1 = self.lstm_cell_1(input_1, state_1)

            s_t = s_gate * torch.tanh(state_1[1])
            s_t = self.s_fc(s_t)

            regions = torch.cat([s_t.unsqueeze(1), detections], 1)

            det_curr, det_next = update_loop(b_s, regions, device, t, seq_len, det_ids)

            det_weights = torch.tanh(self.att_va(det_curr) + self.att_ha(state_1[0]).unsqueeze(1))
            det_weights = self.att_a(det_weights)

            sent_weights = torch.tanh(self.att_s(s_t) + self.att_ha(state_1[0]))
            sent_weights = self.att_a(sent_weights).unsqueeze(1)

            att_weights = F.softmax(torch.cat([sent_weights, det_weights], 1), 1)
            att_outputs.append(F.log_softmax(torch.cat([sent_weights, det_weights], 1), 1).squeeze(-1).unsqueeze(1))

            att_regions = torch.cat([s_t.unsqueeze(1), det_curr], 1)
            att_detections = torch.sum(att_regions * att_weights, 1)

            if not self.img_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1)], 1)
            elif self.img_second_lstm and self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1), image_descriptor], 1)
            elif self.img_second_lstm and not self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, image_descriptor], 1)

            state_2 = self.lstm_cell_2(input_2, state_2)
            out = F.log_softmax(self.out_fc(state_2[0]), dim=-1)
            outputs.append(out.unsqueeze(1))

        return torch.cat(outputs, 1), torch.cat(att_outputs, 1)

    def test(self, detections, images, det_ids, seq_len, bos_idx):
        device = detections.device
        b_s = detections.size(0)
        if images is not None:
            image_descriptor = images
        else:
            detections_mask = (torch.sum(detections, -1, keepdim=True) != 0).float()
            image_descriptor = torch.sum(detections, 1) / torch.sum(detections_mask, 1)

        det_sequence = torch.zeros(b_s, seq_len).long().to(device)
        for i in range(b_s):
            t_out = -1
            for t in range(det_ids.shape[-1]):
                if det_ids[i, t] != 0 and det_ids[i, t] != det_sequence[i, t_out]:
                    t_out += 1
                    det_sequence[i, t_out] = det_ids[i, t]

        index_curr_det = torch.zeros((b_s,)).long().to(device)

        state_1, state_2 = self.init_state(b_s, device)
        ids_prev = torch.zeros((b_s,)).long().to(device)
        prev_selection = torch.zeros((b_s,)).to(device)
        outputs = []

        for t in range(seq_len-1):
            if t == 0:
                it = detections.data.new_full((b_s,), bos_idx).long()
            else:
                it = torch.max(outputs[-1].squeeze(1), -1)[1]
            xt = self.embed(it)

            if self.h2_fist_lstm:
                input_1 = torch.cat([state_2[0], image_descriptor, xt], 1)
            else:
                input_1 = torch.cat([image_descriptor, xt], 1)

            s_gate = torch.sigmoid(self.W1_is(input_1) + self.W1_hs(state_1[0]))
            state_1 = self.lstm_cell_1(input_1, state_1)

            s_t = s_gate * torch.tanh(state_1[1])
            s_t = self.s_fc(s_t)

            regions = torch.cat([s_t.unsqueeze(1), detections], 1)

            det_curr = torch.zeros(b_s, 1, regions.size(-1)).to(device)
            det_next = torch.zeros(b_s, 1, regions.size(-1)).to(device)

            for i in range(b_s):
                if prev_selection[i] == 0:
                    pass
                if det_sequence[i, index_curr_det[i]] == ids_prev[i]:
                    pass
                if prev_selection[i] == 0 and det_sequence[i, index_curr_det[i]] == ids_prev[i]:
                    index_curr_det[i] += 1

                det_curr[i] = regions[i][det_sequence[i, index_curr_det[i]]]
                det_next[i] = regions[i][det_sequence[i, index_curr_det[i] + 1]]

            det_weights = torch.tanh(self.att_va(det_curr) + self.att_ha(state_1[0]).unsqueeze(1))
            det_weights = self.att_a(det_weights)

            sent_weights = torch.tanh(self.att_s(s_t) + self.att_ha(state_1[0]))
            sent_weights = self.att_a(sent_weights).unsqueeze(1)

            att_weights = F.softmax(torch.cat([sent_weights, det_weights], 1), 1)
            prev_selection = torch.argmax(att_weights.squeeze(-1), -1)
            ids_prev = torch.diag(torch.index_select(det_sequence, 1, index_curr_det)) * prev_selection + ids_prev * (1-prev_selection)

            att_regions = torch.cat([s_t.unsqueeze(1), det_curr], 1)
            att_detections = torch.sum(att_regions * att_weights, 1)

            if not self.img_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1)], 1)
            elif self.img_second_lstm and self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1), image_descriptor], 1)
            elif self.img_second_lstm and not self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, image_descriptor], 1)

            state_2 = self.lstm_cell_2(input_2, state_2)
            out = F.log_softmax(self.out_fc(state_2[0]), dim=-1)
            outputs.append(out.unsqueeze(1))

        return torch.max(torch.cat(outputs, 1), -1)[1]

    def test_train(self, detections, images, det_ids, seq_len, bos_idx):
        device = detections.device
        b_s = detections.size(0)
        if images is not None:
            image_descriptor = images
        else:
            detections_mask = (torch.sum(detections, -1, keepdim=True) != 0).float()
            image_descriptor = torch.sum(detections, 1) / torch.sum(detections_mask, 1)

        det_ids_pad = torch.zeros(b_s, seq_len, det_ids.size(-1)).long().to(device)
        if det_ids.size(1) < seq_len:
            det_ids_pad[:, :det_ids.size(1)] = det_ids
        else:
            det_ids_pad = det_ids[:, :seq_len]

        state_1, state_2 = self.init_state(b_s, device)
        outputs = []

        for t in range(seq_len-1):
            if t == 0:
                it = detections.data.new_full((b_s,), bos_idx).long()
            else:
                it = torch.max(outputs[-1].squeeze(1), -1)[1]
            xt = self.embed(it)

            if self.h2_fist_lstm:
                input_1 = torch.cat([state_2[0], image_descriptor, xt], 1)
            else:
                input_1 = torch.cat([image_descriptor, xt], 1)

            s_gate = torch.sigmoid(self.W1_is(input_1) + self.W1_hs(state_1[0]))
            state_1 = self.lstm_cell_1(input_1, state_1)

            s_t = s_gate * torch.tanh(state_1[1])
            s_t = self.s_fc(s_t)

            regions = torch.cat([s_t.unsqueeze(1), detections], 1)

            det_curr, det_next = update_loop(b_s, regions, device, t, seq_len, det_ids_pad)

            det_weights = torch.tanh(self.att_va(det_curr) + self.att_ha(state_1[0]).unsqueeze(1))
            det_weights = self.att_a(det_weights)

            sent_weights = torch.tanh(self.att_s(s_t) + self.att_ha(state_1[0]))
            sent_weights = self.att_a(sent_weights).unsqueeze(1)

            att_weights = F.softmax(torch.cat([sent_weights, det_weights], 1), 1)
            att_regions = torch.cat([s_t.unsqueeze(1), det_curr], 1)
            att_detections = torch.sum(att_regions * att_weights, 1)

            if not self.img_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1)], 1)
            elif self.img_second_lstm and self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1), image_descriptor], 1)
            elif self.img_second_lstm and not self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, image_descriptor], 1)

            state_2 = self.lstm_cell_2(input_2, state_2)
            out = F.log_softmax(self.out_fc(state_2[0]), dim=-1)
            outputs.append(out.unsqueeze(1))

        return torch.max(torch.cat(outputs, 1), -1)[1]


class CocoEntitiesAttention(CaptioningModel):
    def __init__(self, vocab_size, det_feat_size=2048, input_encoding_size=1000, rnn_size=1000, att_size=512, ss_prob=.0,
                 h2_fist_lstm=True, det_next_second_lstm=True, img_second_lstm=False):
        super(CocoEntitiesAttention, self).__init__()
        self.vocab_size = vocab_size
        self.det_feat_size = det_feat_size
        self.input_encoding_size = input_encoding_size
        self.rnn_size = rnn_size
        self.att_size = att_size
        self.h2_fist_lstm = h2_fist_lstm
        self.det_next_second_lstm = det_next_second_lstm
        self.img_second_lstm = img_second_lstm

        self.embed = nn.Embedding(vocab_size, input_encoding_size)

        if self.h2_fist_lstm:
            self.W1_is = nn.Linear(det_feat_size + rnn_size + input_encoding_size, rnn_size)
        else:
            self.W1_is = nn.Linear(det_feat_size + input_encoding_size, rnn_size)
        self.W1_hs = nn.Linear(rnn_size, rnn_size)

        self.att_va = nn.Linear(det_feat_size, att_size, bias=False)
        self.att_ha = nn.Linear(rnn_size, att_size, bias=False)
        self.att_a = nn.Linear(att_size, 1, bias=False)

        self.att_s = nn.Linear(det_feat_size, att_size, bias=False)

        if self.h2_fist_lstm:
            self.lstm_cell_1 = nn.LSTMCell(det_feat_size + rnn_size + input_encoding_size, rnn_size)
        else:
            self.lstm_cell_1 = nn.LSTMCell(det_feat_size + input_encoding_size, rnn_size)

        if self.det_next_second_lstm and self.img_second_lstm:
            self.lstm_cell_2 = nn.LSTMCell(rnn_size + det_feat_size + det_feat_size + det_feat_size, rnn_size)
        elif self.det_next_second_lstm ^ self.img_second_lstm:
            self.lstm_cell_2 = nn.LSTMCell(rnn_size + det_feat_size + det_feat_size, rnn_size)

        self.out_fc = nn.Linear(rnn_size, vocab_size)
        self.s_fc = nn.Linear(rnn_size, det_feat_size)

        self.ss_prob = ss_prob
        self.init_weights()

    def init_weights(self):
        nn.init.xavier_normal_(self.embed.weight)
        nn.init.xavier_normal_(self.out_fc.weight)
        nn.init.constant_(self.out_fc.bias, 0)

        nn.init.xavier_normal_(self.s_fc.weight)
        nn.init.constant_(self.s_fc.bias, 0)

        nn.init.xavier_normal_(self.att_va.weight)
        nn.init.xavier_normal_(self.att_ha.weight)
        nn.init.xavier_normal_(self.att_a.weight)
        nn.init.xavier_normal_(self.att_s.weight)

        nn.init.xavier_normal_(self.lstm_cell_1.weight_ih)
        nn.init.orthogonal_(self.lstm_cell_1.weight_hh)
        nn.init.constant_(self.lstm_cell_1.bias_ih, 0)
        nn.init.constant_(self.lstm_cell_1.bias_hh, 0)

        nn.init.xavier_normal_(self.lstm_cell_2.weight_ih)
        nn.init.orthogonal_(self.lstm_cell_2.weight_hh)
        nn.init.constant_(self.lstm_cell_2.bias_ih, 0)
        nn.init.constant_(self.lstm_cell_2.bias_hh, 0)

    def init_state(self, b_s, device):
        h0_1 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        c0_1 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        h0_2 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        c0_2 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        return (h0_1, c0_1), (h0_2, c0_2)

    def forward(self, detections, images, seq, det_ids):
        device = detections.device
        b_s = detections.size(0)
        seq_len = seq.size(1)
        if images is not None:
            image_descriptor = images
        else:
            detections_mask = (torch.sum(detections, -1, keepdim=True) != 0).float()
            image_descriptor = torch.sum(detections, 1) / torch.sum(detections_mask, 1)

        state_1, state_2 = self.init_state(b_s, device)
        outputs = []
        att_outputs = []

        for t in range(seq_len-1):
            # if self.training and t > 0 and self.ss_prob > .0:
            #     # Scheduled sampling
            #     coin = detections.data.new(b_s).uniform_(0, 1)
            #     coin = (coin < self.ss_prob).long()
            #     distr = distributions.Categorical(logits=outputs[-1].squeeze(1))
            #     action = distr.sample()
            #     it = coin * action.data + (1-coin) * seq[:, t-1].data
            #     it = it.to(device)
            # else:
            it = seq[:, t]
            xt = self.embed(it)

            if self.h2_fist_lstm:
                input_1 = torch.cat([state_2[0], image_descriptor, xt], 1)
            else:
                input_1 = torch.cat([image_descriptor, xt], 1)
            s_gate = torch.sigmoid(self.W1_is(input_1) + self.W1_hs(state_1[0]))
            state_1 = self.lstm_cell_1(input_1, state_1)

            s_t = s_gate * torch.tanh(state_1[1])
            s_t = self.s_fc(s_t)

            regions = torch.cat([s_t.unsqueeze(1), detections], 1)

            det_curr, det_curr_mask, det_next = update_loop_coco(b_s, regions, device, t, seq_len, det_ids)

            det_weights = torch.tanh(self.att_va(det_curr) + self.att_ha(state_1[0]).unsqueeze(1))
            det_weights = self.att_a(det_weights)

            sent_weights = torch.tanh(self.att_s(s_t) + self.att_ha(state_1[0]))
            sent_weights = self.att_a(sent_weights).unsqueeze(1)

            att_weights = F.softmax(torch.cat([sent_weights, det_weights], 1), 1)
            att_weights = att_weights * torch.cat([torch.ones((b_s, 1)).to(device), det_curr_mask.float()], 1).unsqueeze(-1)
            att_weights = att_weights / torch.sum(att_weights, 1, keepdim=True)

            att_outputs.append(torch.sigmoid(torch.cat([sent_weights, det_weights], 1)).squeeze(-1).unsqueeze(1))

            att_regions = torch.cat([s_t.unsqueeze(1), det_curr], 1)
            att_detections = torch.sum(att_regions * att_weights, 1)

            if not self.img_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1)], 1)
            elif self.img_second_lstm and self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1), image_descriptor], 1)
            elif self.img_second_lstm and not self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, image_descriptor], 1)

            state_2 = self.lstm_cell_2(input_2, state_2)
            out = F.log_softmax(self.out_fc(state_2[0]), dim=-1)
            outputs.append(out.unsqueeze(1))

        return torch.cat(outputs, 1), torch.cat(att_outputs, 1)

    def test(self, detections, images, det_ids, seq_len, bos_idx):
        device = detections.device
        b_s = detections.size(0)
        if images is not None:
            image_descriptor = images
        else:
            detections_mask = (torch.sum(detections, -1, keepdim=True) != 0).float()
            image_descriptor = torch.sum(detections, 1) / torch.sum(detections_mask, 1)

        det_sequence = torch.zeros(b_s, seq_len, det_ids.shape[-1]).long().to(device)
        for i in range(b_s):
            t_out = -1
            for t in range(det_ids.shape[1]):
                if det_ids[i, t, 0] != 0 and det_ids[i, t, 0] != det_sequence[i, t_out, 0]:
                    t_out += 1
                    det_sequence[i, t_out] = det_ids[i, t]

        index_curr_det = torch.zeros((b_s,)).long().to(device)

        state_1, state_2 = self.init_state(b_s, device)
        ids_prev = torch.zeros((b_s,)).long().to(device)
        prev_selection = torch.zeros((b_s,)).to(device)
        outputs = []

        for t in range(seq_len-1):
            if t == 0:
                it = detections.data.new_full((b_s,), bos_idx).long()
            else:
                it = torch.max(outputs[-1].squeeze(1), -1)[1]
            xt = self.embed(it)

            if self.h2_fist_lstm:
                input_1 = torch.cat([state_2[0], image_descriptor, xt], 1)
            else:
                input_1 = torch.cat([image_descriptor, xt], 1)
            s_gate = torch.sigmoid(self.W1_is(input_1) + self.W1_hs(state_1[0]))
            state_1 = self.lstm_cell_1(input_1, state_1)

            s_t = s_gate * torch.tanh(state_1[1])
            s_t = self.s_fc(s_t)

            regions = torch.cat([s_t.unsqueeze(1), detections], 1)

            det_curr = torch.zeros(b_s, det_ids.shape[-1]-1, regions.size(-1)).to(device)
            det_curr_mask = torch.zeros(b_s, det_ids.shape[-1]-1, 1).to(device)
            det_next = torch.zeros(b_s, 1, regions.size(-1)).to(device)

            for i in range(b_s):
                if prev_selection[i] == 0:
                    pass
                if det_sequence[i, index_curr_det[i], 0] == ids_prev[i]:
                    pass
                if prev_selection[i] == 0 and det_sequence[i, index_curr_det[i], 0] == ids_prev[i]:
                    index_curr_det[i] += 1

                det_curr[i] = regions[i][det_sequence[i, index_curr_det[i], 1:]]
                det_curr_mask[i] = (det_sequence[i, index_curr_det[i], 1:] != -1).unsqueeze(-1)
                try:
                    det_next[i] = regions[i][det_sequence[i, torch.clamp(index_curr_det[i] + 1, 0, det_sequence.shape[1]-1), 0]]
                except:
                    print(i)
                    print(det_next.shape)
                    print(regions.shape)
                    print(det_sequence.shape)
                    print(index_curr_det.shape)
                    print(index_curr_det[i])
                    print(det_sequence[i, index_curr_det[i] + 1, 0])

            det_weights = torch.tanh(self.att_va(det_curr) + self.att_ha(state_1[0]).unsqueeze(1))
            det_weights = self.att_a(det_weights)

            sent_weights = torch.tanh(self.att_s(s_t) + self.att_ha(state_1[0]))
            sent_weights = self.att_a(sent_weights).unsqueeze(1)

            att_weights = F.softmax(torch.cat([sent_weights, det_weights], 1), 1)
            att_weights = att_weights * torch.cat([torch.ones((b_s, 1, 1)).to(device), det_curr_mask.float()], 1)
            att_weights = att_weights / torch.sum(att_weights, 1, keepdim=True)

            prev_selection = torch.clamp(torch.argmax(att_weights.squeeze(-1), -1), 0, 1)
            ids_prev = torch.diag(torch.index_select(det_sequence[:, :, 0], 1, index_curr_det)) * prev_selection + ids_prev * (1-prev_selection)

            att_regions = torch.cat([s_t.unsqueeze(1), det_curr], 1)
            att_detections = torch.sum(att_regions * att_weights, 1)

            if not self.img_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1)], 1)
            elif self.img_second_lstm and self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1), image_descriptor], 1)
            elif self.img_second_lstm and not self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, image_descriptor], 1)

            state_2 = self.lstm_cell_2(input_2, state_2)
            out = F.log_softmax(self.out_fc(state_2[0]), dim=-1)
            outputs.append(out.unsqueeze(1))

        return torch.max(torch.cat(outputs, 1), -1)[1]

    def test_train(self, detections, images, det_ids, seq_len, bos_idx):
        device = detections.device
        b_s = detections.size(0)
        if images is not None:
            image_descriptor = images
        else:
            detections_mask = (torch.sum(detections, -1, keepdim=True) != 0).float()
            image_descriptor = torch.sum(detections, 1) / torch.sum(detections_mask, 1)

        det_ids_pad = torch.zeros(b_s, seq_len, det_ids.size(-1)).long().to(device)
        if det_ids.size(1) < seq_len:
            det_ids_pad[:, :det_ids.size(1)] = det_ids
        else:
            det_ids_pad = det_ids[:, :seq_len]

        state_1, state_2 = self.init_state(b_s, device)
        outputs = []

        for t in range(seq_len-1):
            if t == 0:
                it = detections.data.new_full((b_s,), bos_idx).long()
            else:
                it = torch.max(outputs[-1].squeeze(1), -1)[1]
            xt = self.embed(it)

            if self.h2_fist_lstm:
                input_1 = torch.cat([state_2[0], image_descriptor, xt], 1)
            else:
                input_1 = torch.cat([image_descriptor, xt], 1)
            s_gate = torch.sigmoid(self.W1_is(input_1) + self.W1_hs(state_1[0]))
            state_1 = self.lstm_cell_1(input_1, state_1)

            s_t = s_gate * torch.tanh(state_1[1])
            s_t = self.s_fc(s_t)

            regions = torch.cat([s_t.unsqueeze(1), detections], 1)

            det_curr, det_curr_mask, det_next = update_loop_coco(b_s, regions, device, t, seq_len, det_ids_pad)

            det_weights = torch.tanh(self.att_va(det_curr) + self.att_ha(state_1[0]).unsqueeze(1))
            det_weights = self.att_a(det_weights)

            sent_weights = torch.tanh(self.att_s(s_t) + self.att_ha(state_1[0]))
            sent_weights = self.att_a(sent_weights).unsqueeze(1)

            att_weights = F.softmax(torch.cat([sent_weights, det_weights], 1), 1)
            att_weights = att_weights * torch.cat([torch.ones((b_s, 1)).to(device), det_curr_mask.float()],
                                                  1).unsqueeze(-1)
            att_weights = att_weights / torch.sum(att_weights, 1, keepdim=True)

            att_regions = torch.cat([s_t.unsqueeze(1), det_curr], 1)
            att_detections = torch.sum(att_regions * att_weights, 1)

            if not self.img_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1)], 1)
            elif self.img_second_lstm and self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1), image_descriptor], 1)
            elif self.img_second_lstm and not self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, image_descriptor], 1)

            state_2 = self.lstm_cell_2(input_2, state_2)
            out = F.log_softmax(self.out_fc(state_2[0]), dim=-1)
            outputs.append(out.unsqueeze(1))

        return torch.max(torch.cat(outputs, 1), -1)[1]


class EntitiesAttentionImproved_LSTM(CaptioningModel):
    def __init__(self, vocab_size, det_feat_size=2048, input_encoding_size=1000, rnn_size=1000, rnn_det_next_size=2048, att_size=512, ss_prob=.0):
        super(EntitiesAttentionImproved_LSTM, self).__init__()
        self.vocab_size = vocab_size
        self.det_feat_size = det_feat_size
        self.input_encoding_size = input_encoding_size
        self.rnn_size = rnn_size
        self.att_size = att_size
        self.rnn_det_next_size = rnn_det_next_size

        self.embed = nn.Embedding(vocab_size, input_encoding_size)

        self.W1_is = nn.Linear(det_feat_size + rnn_size + input_encoding_size, rnn_size)
        self.W1_hs = nn.Linear(rnn_size, rnn_size)

        self.att_va = nn.Linear(det_feat_size, att_size, bias=False)
        self.att_ha = nn.Linear(rnn_size, att_size, bias=False)
        self.att_a = nn.Linear(att_size, 1, bias=False)

        self.att_s = nn.Linear(det_feat_size, att_size, bias=False)

        self.lstm_cell_1 = nn.LSTMCell(det_feat_size + rnn_size + input_encoding_size, rnn_size)
        self.lstm_cell_2 = nn.LSTMCell(rnn_size + det_feat_size + rnn_det_next_size, rnn_size)

        # self.lstm_det_next = nn.LSTM(det_feat_size, rnn_det_next_size, num_layers=1)
        self.rnn_det_next = nn.RNN(input_size=det_feat_size, hidden_size=rnn_det_next_size, num_layers=1)

        self.out_fc = nn.Linear(rnn_size, vocab_size)
        self.s_fc = nn.Linear(rnn_size, det_feat_size)

        self.ss_prob = ss_prob
        self.init_weights()

    def init_weights(self):
        nn.init.xavier_normal_(self.embed.weight)
        nn.init.xavier_normal_(self.out_fc.weight)
        nn.init.constant_(self.out_fc.bias, 0)

        nn.init.xavier_normal_(self.s_fc.weight)
        nn.init.constant_(self.s_fc.bias, 0)

        nn.init.xavier_normal_(self.att_va.weight)
        nn.init.xavier_normal_(self.att_ha.weight)
        nn.init.xavier_normal_(self.att_a.weight)
        nn.init.xavier_normal_(self.att_s.weight)

        nn.init.xavier_normal_(self.lstm_cell_1.weight_ih)
        nn.init.orthogonal_(self.lstm_cell_1.weight_hh)
        nn.init.constant_(self.lstm_cell_1.bias_ih, 0)
        nn.init.constant_(self.lstm_cell_1.bias_hh, 0)

        nn.init.xavier_normal_(self.lstm_cell_2.weight_ih)
        nn.init.orthogonal_(self.lstm_cell_2.weight_hh)
        nn.init.constant_(self.lstm_cell_2.bias_ih, 0)
        nn.init.constant_(self.lstm_cell_2.bias_hh, 0)

        nn.init.xavier_normal_(self.rnn_det_next.weight_ih_l0)
        nn.init.orthogonal_(self.rnn_det_next.weight_hh_l0)
        nn.init.constant_(self.rnn_det_next.bias_ih_l0, 0)
        nn.init.constant_(self.rnn_det_next.bias_hh_l0, 0)

    def init_state(self, b_s, device):
        h0_1 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        c0_1 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        h0_2 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        c0_2 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        return (h0_1, c0_1), (h0_2, c0_2)

    def init_state_det_next(self, b_s, device):
        h0 = torch.zeros((1, b_s, self.rnn_det_next_size), requires_grad=True).to(device)
        c0 = torch.zeros((1, b_s, self.rnn_det_next_size), requires_grad=True).to(device)
        return h0, c0

    def forward(self, detections, images, seq, det_ids, mask_det_next=False):
        device = detections.device
        b_s = detections.size(0)
        seq_len = seq.size(1)
        if images is not None:
            image_descriptor = images
        else:
            detections_mask = (torch.sum(detections, -1, keepdim=True) != 0).float()
            image_descriptor = torch.sum(detections, 1) / torch.sum(detections_mask, 1)

        state_1, state_2 = self.init_state(b_s, device)
        outputs = []
        att_outputs = []

        for t in range(seq_len-1):
            if self.training and t > 0 and self.ss_prob > .0:
                # Scheduled sampling
                coin = detections.data.new(b_s).uniform_(0, 1)
                coin = (coin < self.ss_prob).long()
                distr = distributions.Categorical(logits=outputs[-1].squeeze(1))
                action = distr.sample()
                it = coin * action.data + (1-coin) * seq[:, t-1].data
                it = it.to(device)
            else:
                it = seq[:, t]
            xt = self.embed(it)

            input_1 = torch.cat([state_2[0], image_descriptor, xt], 1)
            s_gate = torch.sigmoid(self.W1_is(input_1) + self.W1_hs(state_1[0]))
            state_1 = self.lstm_cell_1(input_1, state_1)

            s_t = s_gate * torch.tanh(state_1[1])
            s_t = self.s_fc(s_t)

            regions = torch.cat([s_t.unsqueeze(1), detections], 1)

            det_curr, det_next = update_loop_with_lstm(b_s, regions, device, t, seq_len, det_ids)

            det_weights = torch.tanh(self.att_va(det_curr) + self.att_ha(state_1[0]).unsqueeze(1))
            det_weights = self.att_a(det_weights)

            sent_weights = torch.tanh(self.att_s(s_t) + self.att_ha(state_1[0]))
            sent_weights = self.att_a(sent_weights).unsqueeze(1)

            att_weights = F.softmax(torch.cat([sent_weights, det_weights], 1), 1)
            att_outputs.append(F.log_softmax(torch.cat([sent_weights, det_weights], 1), 1).squeeze(-1).unsqueeze(1))

            att_regions = torch.cat([s_t.unsqueeze(1), det_curr], 1)
            att_detections = torch.sum(att_regions * att_weights, 1)

            state_3, _ = self.init_state_det_next(b_s, device)

            det_next_ids, det_next_regions = det_next
            lengths = [d.size(0) for d in det_next_ids]
            argsort = np.argsort(lengths)[::-1].tolist()
            det_next_regions_sorted = [det_next_regions[i] for i in argsort]

            det_next_sorted = nn.utils.rnn.pack_sequence(det_next_regions_sorted)
            out_lstm = self.rnn_det_next(det_next_sorted, state_3)[1][0].squeeze(0)

            argsort = torch.from_numpy(np.argsort(argsort)).to(device)
            next_detections = out_lstm[argsort]

            if mask_det_next:
                mask = torch.tensor([sum(d)!=0 for d in det_next_ids]).float().to(device)
                mask = mask.unsqueeze(-1).expand((b_s, regions.size(-1)))
                next_detections = mask * next_detections + (1 - mask) * regions[:, 0]

            input_2 = torch.cat([state_1[0], att_detections, next_detections], 1)
            state_2 = self.lstm_cell_2(input_2, state_2)
            out = F.log_softmax(self.out_fc(state_2[0]), dim=-1)
            outputs.append(out.unsqueeze(1))

        return torch.cat(outputs, 1), torch.cat(att_outputs, 1)

    def test(self, detections, images, det_ids, seq_len, bos_idx, mask_det_next=False):
        device = detections.device
        b_s = detections.size(0)
        if images is not None:
            image_descriptor = images
        else:
            detections_mask = (torch.sum(detections, -1, keepdim=True) != 0).float()
            image_descriptor = torch.sum(detections, 1) / torch.sum(detections_mask, 1)

        det_sequence = torch.zeros(b_s, seq_len).long().to(device)
        det_sequence_ids = []
        for i in range(b_s):
            t_out = -1
            ids_i = []
            for t in range(det_ids.shape[-1]):
                if det_ids[i, t] != 0 and det_ids[i, t] != det_sequence[i, t_out]:
                    t_out += 1
                    ids_i.append(det_ids[i, t])
                    det_sequence[i, t_out] = det_ids[i, t]
            det_sequence_ids.append(torch.tensor(ids_i).long().to(device))

        index_curr_det = torch.zeros((b_s,)).long().to(device)

        state_1, state_2 = self.init_state(b_s, device)
        ids_prev = torch.zeros((b_s,)).long().to(device)
        prev_selection = torch.zeros((b_s,)).to(device)
        outputs = []

        for t in range(seq_len-1):
            if t == 0:
                it = detections.data.new_full((b_s,), bos_idx).long()
            else:
                it = torch.max(outputs[-1].squeeze(1), -1)[1]
            xt = self.embed(it)

            input_1 = torch.cat([state_2[0], image_descriptor, xt], 1)
            s_gate = torch.sigmoid(self.W1_is(input_1) + self.W1_hs(state_1[0]))
            state_1 = self.lstm_cell_1(input_1, state_1)

            s_t = s_gate * torch.tanh(state_1[1])
            s_t = self.s_fc(s_t)

            regions = torch.cat([s_t.unsqueeze(1), detections], 1)

            det_curr = torch.zeros(b_s, 1, regions.size(-1)).to(device)
            det_next_ids = []
            det_next_regions = []
            for i in range(b_s):
                if prev_selection[i] == 0:
                    pass
                if det_sequence[i, index_curr_det[i]] == ids_prev[i]:
                    pass
                if prev_selection[i] == 0 and det_sequence[i, index_curr_det[i]] == ids_prev[i]:
                    index_curr_det[i] += 1

                det_curr[i] = regions[i][det_sequence[i, index_curr_det[i]]]

                if (index_curr_det[i] + 1) >= det_sequence_ids[i].size(0):
                    valid = torch.zeros(1).long().to(device)
                else:
                    valid = det_sequence_ids[i][index_curr_det[i] + 1:]

                valid_regions = torch.index_select(regions[i], 0, valid)
                det_next_ids.append(valid)
                det_next_regions.append(valid_regions)

            det_weights = torch.tanh(self.att_va(det_curr) + self.att_ha(state_1[0]).unsqueeze(1))
            det_weights = self.att_a(det_weights)

            sent_weights = torch.tanh(self.att_s(s_t) + self.att_ha(state_1[0]))
            sent_weights = self.att_a(sent_weights).unsqueeze(1)

            att_weights = F.softmax(torch.cat([sent_weights, det_weights], 1), 1)
            prev_selection = torch.argmax(att_weights.squeeze(-1), -1)
            ids_prev = torch.diag(torch.index_select(det_sequence, 1, index_curr_det)) * prev_selection + ids_prev * (1-prev_selection)

            att_regions = torch.cat([s_t.unsqueeze(1), det_curr], 1)
            att_detections = torch.sum(att_regions * att_weights, 1)

            state_3, _ = self.init_state_det_next(b_s, device)

            lengths = [d.size(0) for d in det_next_ids]
            argsort = np.argsort(lengths)[::-1].tolist()
            det_next_regions_sorted = [det_next_regions[i] for i in argsort]

            det_next_sorted = nn.utils.rnn.pack_sequence(det_next_regions_sorted)
            out_lstm = self.rnn_det_next(det_next_sorted, state_3)[1][0].squeeze(0)

            argsort = torch.from_numpy(np.argsort(argsort)).to(device)
            next_detections = out_lstm[argsort]

            if mask_det_next:
                mask = torch.tensor([sum(d)!=0 for d in det_next_ids]).float().to(device)
                mask = mask.unsqueeze(-1).expand((b_s, regions.size(-1)))
                next_detections = mask * next_detections + (1 - mask) * regions[:, 0]

            input_2 = torch.cat([state_1[0], att_detections, next_detections.squeeze(1)], 1)

            state_2 = self.lstm_cell_2(input_2, state_2)
            out = F.log_softmax(self.out_fc(state_2[0]), dim=-1)
            outputs.append(out.unsqueeze(1))

        return torch.max(torch.cat(outputs, 1), -1)[1]


class EntitiesAttentionFixed(CaptioningModel):
    def __init__(self, vocab_size, det_feat_size=2048, input_encoding_size=1000, rnn_size=1000, att_size=512, ss_prob=.0,
                 h2_fist_lstm=True, det_next_second_lstm=True, img_second_lstm=False):
        super(EntitiesAttentionFixed, self).__init__()
        self.vocab_size = vocab_size
        self.det_feat_size = det_feat_size
        self.input_encoding_size = input_encoding_size
        self.rnn_size = rnn_size
        self.att_size = att_size
        self.h2_fist_lstm = h2_fist_lstm
        self.det_next_second_lstm = det_next_second_lstm
        self.img_second_lstm = img_second_lstm

        self.embed = nn.Embedding(vocab_size, input_encoding_size)

        if self.h2_fist_lstm:
            self.W1_is = nn.Linear(det_feat_size + rnn_size + input_encoding_size, rnn_size)
        else:
            self.W1_is = nn.Linear(det_feat_size + input_encoding_size, rnn_size)
        self.W1_hs = nn.Linear(rnn_size, rnn_size)

        self.att_va = nn.Linear(det_feat_size, att_size, bias=False)
        self.att_ha = nn.Linear(rnn_size, att_size, bias=False)
        self.att_a = nn.Linear(att_size, 1, bias=False)

        self.att_sa = nn.Linear(rnn_size, att_size, bias=False)
        self.att_s = nn.Linear(att_size, 1, bias=False)

        if self.h2_fist_lstm:
            self.lstm_cell_1 = nn.LSTMCell(det_feat_size + rnn_size + input_encoding_size, rnn_size)
        else:
            self.lstm_cell_1 = nn.LSTMCell(det_feat_size + input_encoding_size, rnn_size)

        if self.det_next_second_lstm and self.img_second_lstm:
            self.lstm_cell_2 = nn.LSTMCell(rnn_size + det_feat_size + det_feat_size + det_feat_size, rnn_size)
        elif self.det_next_second_lstm ^ self.img_second_lstm:
            self.lstm_cell_2 = nn.LSTMCell(rnn_size + det_feat_size + det_feat_size, rnn_size)

        self.out_fc = nn.Linear(rnn_size, vocab_size)
        self.s_fc = nn.Linear(rnn_size, det_feat_size)

        self.ss_prob = ss_prob
        self.init_weights()

    def init_weights(self):
        nn.init.xavier_normal_(self.embed.weight)
        nn.init.xavier_normal_(self.out_fc.weight)
        nn.init.constant_(self.out_fc.bias, 0)

        nn.init.xavier_normal_(self.W1_is.weight)
        nn.init.constant_(self.W1_is.bias, 0)
        nn.init.xavier_normal_(self.W1_hs.weight)
        nn.init.constant_(self.W1_hs.bias, 0)

        nn.init.xavier_normal_(self.s_fc.weight)
        nn.init.constant_(self.s_fc.bias, 0)

        nn.init.xavier_normal_(self.att_va.weight)
        nn.init.xavier_normal_(self.att_ha.weight)
        nn.init.xavier_normal_(self.att_a.weight)
        nn.init.xavier_normal_(self.att_sa.weight)
        nn.init.xavier_normal_(self.att_s.weight)

        nn.init.xavier_normal_(self.lstm_cell_1.weight_ih)
        nn.init.orthogonal_(self.lstm_cell_1.weight_hh)
        nn.init.constant_(self.lstm_cell_1.bias_ih, 0)
        nn.init.constant_(self.lstm_cell_1.bias_hh, 0)

        nn.init.xavier_normal_(self.lstm_cell_2.weight_ih)
        nn.init.orthogonal_(self.lstm_cell_2.weight_hh)
        nn.init.constant_(self.lstm_cell_2.bias_ih, 0)
        nn.init.constant_(self.lstm_cell_2.bias_hh, 0)

    def init_state(self, b_s, device):
        h0_1 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        c0_1 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        h0_2 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        c0_2 = torch.zeros((b_s, self.rnn_size), requires_grad=True).to(device)
        return (h0_1, c0_1), (h0_2, c0_2)

    def forward(self, detections, images, seq, det_ids):
        device = detections.device
        b_s = detections.size(0)
        seq_len = seq.size(1)
        if images is not None:
            image_descriptor = images
        else:
            detections_mask = (torch.sum(detections, -1, keepdim=True) != 0).float()
            image_descriptor = torch.sum(detections, 1) / torch.sum(detections_mask, 1)

        state_1, state_2 = self.init_state(b_s, device)
        outputs = []
        att_outputs = []

        for t in range(seq_len-1):
            # if self.training and t > 0 and self.ss_prob > .0:
            #     # Scheduled sampling
            #     coin = detections.data.new(b_s).uniform_(0, 1)
            #     coin = (coin < self.ss_prob).long()
            #     distr = distributions.Categorical(logits=outputs[-1].squeeze(1))
            #     action = distr.sample()
            #     it = coin * action.data + (1-coin) * seq[:, t-1].data
            #     it = it.to(device)
            # else:
            it = seq[:, t]
            xt = self.embed(it)

            if self.h2_fist_lstm:
                input_1 = torch.cat([state_2[0], image_descriptor, xt], 1)
            else:
                input_1 = torch.cat([image_descriptor, xt], 1)

            s_gate = torch.sigmoid(self.W1_is(input_1) + self.W1_hs(state_1[0]))
            state_1 = self.lstm_cell_1(input_1, state_1)

            s_t = s_gate * torch.tanh(state_1[1])
            sentinel_fc = self.s_fc(s_t)

            regions = torch.cat([sentinel_fc.unsqueeze(1), detections], 1)

            det_curr, det_next = update_loop(b_s, regions, device, t, seq_len, det_ids)

            det_weights = torch.tanh(self.att_va(det_curr) + self.att_ha(state_1[0]).unsqueeze(1))
            det_weights = self.att_a(det_weights)

            sent_weights = torch.tanh(self.att_sa(s_t) + self.att_ha(state_1[0]))
            sent_weights = self.att_s(sent_weights).unsqueeze(1)

            att_weights = F.softmax(torch.cat([sent_weights, det_weights], 1), 1)
            att_outputs.append(F.log_softmax(torch.cat([sent_weights, det_weights], 1), 1).squeeze(-1).unsqueeze(1))

            att_regions = torch.cat([sentinel_fc.unsqueeze(1), det_curr], 1)
            att_detections = torch.sum(att_regions * att_weights, 1)

            if not self.img_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1)], 1)
            elif self.img_second_lstm and self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1), image_descriptor], 1)
            elif self.img_second_lstm and not self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, image_descriptor], 1)

            state_2 = self.lstm_cell_2(input_2, state_2)
            out = F.log_softmax(self.out_fc(state_2[0]), dim=-1)
            outputs.append(out.unsqueeze(1))

        return torch.cat(outputs, 1), torch.cat(att_outputs, 1)

    def test(self, detections, images, det_ids, seq_len, bos_idx):
        device = detections.device
        b_s = detections.size(0)
        if images is not None:
            image_descriptor = images
        else:
            detections_mask = (torch.sum(detections, -1, keepdim=True) != 0).float()
            image_descriptor = torch.sum(detections, 1) / torch.sum(detections_mask, 1)

        det_sequence = torch.zeros(b_s, seq_len).long().to(device)
        for i in range(b_s):
            t_out = -1
            for t in range(det_ids.shape[-1]):
                if det_ids[i, t] != 0 and det_ids[i, t] != det_sequence[i, t_out]:
                    t_out += 1
                    det_sequence[i, t_out] = det_ids[i, t]

        index_curr_det = torch.zeros((b_s,)).long().to(device)

        state_1, state_2 = self.init_state(b_s, device)
        ids_prev = torch.zeros((b_s,)).long().to(device)
        prev_selection = torch.zeros((b_s,)).to(device)
        outputs = []

        for t in range(seq_len-1):
            if t == 0:
                it = detections.data.new_full((b_s,), bos_idx).long()
            else:
                it = torch.max(outputs[-1].squeeze(1), -1)[1]
            xt = self.embed(it)

            if self.h2_fist_lstm:
                input_1 = torch.cat([state_2[0], image_descriptor, xt], 1)
            else:
                input_1 = torch.cat([image_descriptor, xt], 1)

            s_gate = torch.sigmoid(self.W1_is(input_1) + self.W1_hs(state_1[0]))
            state_1 = self.lstm_cell_1(input_1, state_1)

            s_t = s_gate * torch.tanh(state_1[1])
            sentinel_fc = self.s_fc(s_t)

            regions = torch.cat([sentinel_fc.unsqueeze(1), detections], 1)

            det_curr = torch.zeros(b_s, 1, regions.size(-1)).to(device)
            det_next = torch.zeros(b_s, 1, regions.size(-1)).to(device)

            for i in range(b_s):
                if prev_selection[i] == 0:
                    pass
                if det_sequence[i, index_curr_det[i]] == ids_prev[i]:
                    pass
                if prev_selection[i] == 0 and det_sequence[i, index_curr_det[i]] == ids_prev[i]:
                    index_curr_det[i] += 1

                det_curr[i] = regions[i][det_sequence[i, index_curr_det[i]]]
                det_next[i] = regions[i][det_sequence[i, index_curr_det[i] + 1]]

            det_weights = torch.tanh(self.att_va(det_curr) + self.att_ha(state_1[0]).unsqueeze(1))
            det_weights = self.att_a(det_weights)

            sent_weights = torch.tanh(self.att_sa(s_t) + self.att_ha(state_1[0]))
            sent_weights = self.att_s(sent_weights).unsqueeze(1)

            att_weights = F.softmax(torch.cat([sent_weights, det_weights], 1), 1)
            prev_selection = torch.argmax(att_weights.squeeze(-1), -1)
            ids_prev = torch.diag(torch.index_select(det_sequence, 1, index_curr_det)) * prev_selection + ids_prev * (1-prev_selection)

            att_regions = torch.cat([sentinel_fc.unsqueeze(1), det_curr], 1)
            att_detections = torch.sum(att_regions * att_weights, 1)

            if not self.img_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1)], 1)
            elif self.img_second_lstm and self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1), image_descriptor], 1)
            elif self.img_second_lstm and not self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, image_descriptor], 1)

            state_2 = self.lstm_cell_2(input_2, state_2)
            out = F.log_softmax(self.out_fc(state_2[0]), dim=-1)
            outputs.append(out.unsqueeze(1))

        return torch.max(torch.cat(outputs, 1), -1)[1]

    def test_train(self, detections, images, det_ids, seq_len, bos_idx):
        device = detections.device
        b_s = detections.size(0)
        if images is not None:
            image_descriptor = images
        else:
            detections_mask = (torch.sum(detections, -1, keepdim=True) != 0).float()
            image_descriptor = torch.sum(detections, 1) / torch.sum(detections_mask, 1)

        det_ids_pad = torch.zeros(b_s, seq_len, det_ids.size(-1)).long().to(device)
        if det_ids.size(1) < seq_len:
            det_ids_pad[:, :det_ids.size(1)] = det_ids
        else:
            det_ids_pad = det_ids[:, :seq_len]

        state_1, state_2 = self.init_state(b_s, device)
        outputs = []

        for t in range(seq_len-1):
            if t == 0:
                it = detections.data.new_full((b_s,), bos_idx).long()
            else:
                it = torch.max(outputs[-1].squeeze(1), -1)[1]
            xt = self.embed(it)

            if self.h2_fist_lstm:
                input_1 = torch.cat([state_2[0], image_descriptor, xt], 1)
            else:
                input_1 = torch.cat([image_descriptor, xt], 1)

            s_gate = torch.sigmoid(self.W1_is(input_1) + self.W1_hs(state_1[0]))
            state_1 = self.lstm_cell_1(input_1, state_1)

            s_t = s_gate * torch.tanh(state_1[1])
            sentinel_fc = self.s_fc(s_t)

            regions = torch.cat([sentinel_fc.unsqueeze(1), detections], 1)

            det_curr, det_next = update_loop(b_s, regions, device, t, seq_len, det_ids_pad)

            det_weights = torch.tanh(self.att_va(det_curr) + self.att_ha(state_1[0]).unsqueeze(1))
            det_weights = self.att_a(det_weights)

            sent_weights = torch.tanh(self.att_sa(s_t) + self.att_ha(state_1[0]))
            sent_weights = self.att_s(sent_weights).unsqueeze(1)

            att_weights = F.softmax(torch.cat([sent_weights, det_weights], 1), 1)
            att_regions = torch.cat([sentinel_fc.unsqueeze(1), det_curr], 1)
            att_detections = torch.sum(att_regions * att_weights, 1)

            if not self.img_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1)], 1)
            elif self.img_second_lstm and self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, det_next.squeeze(1), image_descriptor], 1)
            elif self.img_second_lstm and not self.det_next_second_lstm:
                input_2 = torch.cat([state_1[0], att_detections, image_descriptor], 1)

            state_2 = self.lstm_cell_2(input_2, state_2)
            out = F.log_softmax(self.out_fc(state_2[0]), dim=-1)
            outputs.append(out.unsqueeze(1))

        return torch.max(torch.cat(outputs, 1), -1)[1]