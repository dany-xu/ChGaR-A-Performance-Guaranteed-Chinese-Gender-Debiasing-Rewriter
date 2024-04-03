import pandas as pd
import torch
import numpy as np
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
import time
import math
import random
import os
'''
os.system('nvidia-smi -q -d Memory |grep -A4 GPU|grep Free >tmp')
memory_gpu=[int(x.split()[2]) for x in open('tmp','r').readlines()]
os.environ['CUDA_VISIBLE_DEVICES']=str(np.argmax(memory_gpu))
os.system('rm tmp')
'''
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
seed = 2020

new_data = pd.read_csv('new_data_3.csv')

before=[line for line in new_data['before'] if pd.isnull(line)==False]
after=[line for line in new_data['after'] if pd.isnull(line)==False]

before_token_list = [[char for char in line]+["<eos>"] for line in before]
after_token_list = [[char for char in line]+["<eos>"] for line in after]

basic_dict = {'<pad>':0, '<unk>':1, '<bos>':2, '<eos>':3}
before_vocab = set(''.join(before))
before2id = {char:i+len(basic_dict) for i, char in enumerate(before_vocab)}
before2id.update(basic_dict)
id2before = {v:k for k,v in before2id.items()}

after_vocab = set(''.join(after))
after2id = {char:i+len(basic_dict) for i, char in enumerate(after_vocab)}
after2id.update(basic_dict)
id2after = {v:k for k,v in after2id.items()}

before_num_data = [[before2id[b] for b in line ] for line in before_token_list]
after_num_data = [[after2id[a] for a in line] for line in after_token_list]


class TranslationDataset(Dataset):
    def __init__(self, src_data, trg_data):
        self.src_data = src_data
        self.trg_data = trg_data

        assert len(src_data) == len(trg_data), \
            "numbers of src_data  and trg_data must be equal!"

    def __len__(self):
        return len(self.src_data)

    def __getitem__(self, idx):
        src_sample = self.src_data[idx]
        src_len = len(self.src_data[idx])
        trg_sample = self.trg_data[idx]
        trg_len = len(self.trg_data[idx])
        return {"src": src_sample, "src_len": src_len, "trg": trg_sample, "trg_len": trg_len}


def padding_batch(batch):
    """
    input: -> list of dict
        [{'src': [1, 2, 3], 'trg': [1, 2, 3]}, {'src': [1, 2, 2, 3], 'trg': [1, 2, 2, 3]}]
    output: -> dict of tensor
        {
            "src": [[1, 2, 3, 0], [1, 2, 2, 3]].T
            "trg": [[1, 2, 3, 0], [1, 2, 2, 3]].T
        }
    """
    src_lens = [d["src_len"] for d in batch]
    trg_lens = [d["trg_len"] for d in batch]

    src_max = max([d["src_len"] for d in batch])
    trg_max = max([d["trg_len"] for d in batch])
    for d in batch:
        d["src"].extend([before2id["<pad>"]] * (src_max - d["src_len"]))
        d["trg"].extend([after2id["<pad>"]] * (trg_max - d["trg_len"]))
    srcs = torch.tensor([pair["src"] for pair in batch], dtype=torch.long, device=device)
    trgs = torch.tensor([pair["trg"] for pair in batch], dtype=torch.long, device=device)

    batch = {"src": srcs.T, "src_len": src_lens, "trg": trgs.T, "trg_len": trg_lens}
    return batch


class Encoder(nn.Module):
    def __init__(self, input_dim, emb_dim, hid_dim, n_layers, dropout=0.5, bidirectional=True):
        super(Encoder, self).__init__()

        self.hid_dim = hid_dim
        self.n_layers = n_layers

        self.embedding = nn.Embedding(input_dim, emb_dim)
        self.gru = nn.GRU(emb_dim, hid_dim, n_layers, dropout=dropout, bidirectional=bidirectional)

    def forward(self, input_seqs, input_lengths, hidden):
        # input_seqs = [seq_len, batch]
        embedded = self.embedding(input_seqs)
        # embedded = [seq_len, batch, embed_dim]
        packed = torch.nn.utils.rnn.pack_padded_sequence(embedded, input_lengths, enforce_sorted=False)

        outputs, hidden = self.gru(packed, hidden)
        outputs, output_lengths = torch.nn.utils.rnn.pad_packed_sequence(outputs)
        # outputs = [seq_len, batch, hid_dim * n directions]
        # output_lengths = [batch]
        return outputs, hidden


class Decoder(nn.Module):
    def __init__(self, output_dim, emb_dim, hid_dim, n_layers, dropout=0.5, bidirectional=True):
        super(Decoder, self).__init__()

        self.output_dim = output_dim
        self.hid_dim = hid_dim
        self.n_layers = n_layers

        self.embedding = nn.Embedding(output_dim, emb_dim)
        self.gru = nn.GRU(emb_dim, hid_dim, n_layers, dropout=dropout, bidirectional=bidirectional)

        if bidirectional:
            self.fc_out = nn.Linear(hid_dim * 2, output_dim)
        else:
            self.fc_out = nn.Linear(hid_dim, output_dim)
        self.dropout = nn.Dropout(dropout)
        self.softmax = nn.LogSoftmax(dim=1)

    def forward(self, token_inputs, hidden):
        # token_inputs = [batch]
        batch_size = token_inputs.size(0)
        embedded = self.dropout(self.embedding(token_inputs).view(1, batch_size, -1))
        # embedded = [1, batch, emb_dim]

        output, hidden = self.gru(embedded, hidden)
        # output = [1, batch,  n_directions * hid_dim]
        # hidden = [n_layers * n_directions, batch, hid_dim]

        output = self.fc_out(output.squeeze(0))
        output = self.softmax(output)
        # output = [batch, output_dim]
        return output, hidden


class Seq2Seq(nn.Module):
    def __init__(self,
                 encoder,
                 decoder,
                 device,
                 predict=False,
                 basic_dict=None,
                 max_len=100
                 ):
        super(Seq2Seq, self).__init__()

        self.device = device

        self.encoder = encoder
        self.decoder = decoder

        self.predict = predict
        self.basic_dict = basic_dict
        self.max_len = max_len

        self.enc_n_layers = self.encoder.gru.num_layers
        self.enc_n_directions = 2 if self.encoder.gru.bidirectional else 1
        self.dec_n_directions = 2 if self.decoder.gru.bidirectional else 1

        assert encoder.hid_dim == decoder.hid_dim, \
            "Hidden dimensions of encoder and decoder must be equal!"
        assert encoder.n_layers == decoder.n_layers, \
            "Encoder and decoder must have equal number of layers!"
        assert self.enc_n_directions >= self.dec_n_directions, \
            "If decoder is bidirectional, encoder must be bidirectional either!"

    def forward(self, input_batches, input_lengths, target_batches=None, target_lengths=None,
                teacher_forcing_ratio=0.5):
        # input_batches = target_batches = [seq_len, batch]
        batch_size = input_batches.size(1)

        BOS_token = self.basic_dict["<bos>"]
        EOS_token = self.basic_dict["<eos>"]
        PAD_token = self.basic_dict["<pad>"]

        encoder_hidden = torch.zeros(self.enc_n_layers * self.enc_n_directions, batch_size, self.encoder.hid_dim,
                                     device=self.device)

        # encoder_output = [seq_len, batch, hid_dim * n directions]
        # encoder_hidden = [n_layers*n_directions, batch, hid_dim]
        encoder_output, encoder_hidden = self.encoder(
            input_batches, input_lengths, encoder_hidden)

        decoder_input = torch.tensor([BOS_token] * batch_size, dtype=torch.long, device=self.device)
        if self.enc_n_directions == self.dec_n_directions:
            decoder_hidden = encoder_hidden
        else:
            L = encoder_hidden.size(0)
            decoder_hidden = encoder_hidden[range(0, L, 2)] + encoder_hidden[range(1, L, 2)]

        if self.predict:
            assert batch_size == 1, "batch_size of predict phase must be 1!"
            output_tokens = []

            while True:
                decoder_output, decoder_hidden = self.decoder(
                    decoder_input, decoder_hidden
                )
                # [1, 1]
                topv, topi = decoder_output.topk(1)
                decoder_input = topi.squeeze(1)
                output_token = topi.squeeze().detach().item()
                if output_token == EOS_token or len(output_tokens) == self.max_len:
                    break
                output_tokens.append(output_token)
            return output_tokens

        else:
            max_target_length = max(target_lengths)
            all_decoder_outputs = torch.zeros((max_target_length, batch_size, self.decoder.output_dim),
                                              device=self.device)

            for t in range(max_target_length):
                use_teacher_forcing = True if random.random() < teacher_forcing_ratio else False
                if use_teacher_forcing:
                    # decoder_output = [batch, output_dim]
                    # decoder_hidden = [n_layers*n_directions, batch, hid_dim]
                    decoder_output, decoder_hidden = self.decoder(
                        decoder_input, decoder_hidden
                    )
                    all_decoder_outputs[t] = decoder_output
                    decoder_input = target_batches[t]
                else:
                    decoder_output, decoder_hidden = self.decoder(
                        decoder_input, decoder_hidden
                    )
                    # [batch, 1]
                    topv, topi = decoder_output.topk(1)
                    all_decoder_outputs[t] = decoder_output
                    decoder_input = topi.squeeze(1)

            loss_fn = nn.NLLLoss(ignore_index=PAD_token)
            loss = loss_fn(
                all_decoder_outputs.reshape(-1, self.decoder.output_dim),  # [batch*seq_len, output_dim]
                target_batches.reshape(-1)  # [batch*seq_len]
            )
            return loss


def epoch_time(start_time, end_time):
    elapsed_time = end_time - start_time
    elapsed_mins = int(elapsed_time / 60)
    elapsed_secs = int(elapsed_time - (elapsed_mins * 60))
    return elapsed_mins, elapsed_secs


def train(
        model,
        data_loader,
        optimizer,
        clip=1,
        teacher_forcing_ratio=0.5,
        print_every=None
):
    model.predict = False
    model.train()

    if print_every == 0:
        print_every = 1

    print_loss_total = 0
    start = time.time()
    epoch_loss = 0
    for i, batch in enumerate(data_loader):

        # shape = [seq_len, batch]
        input_batchs = batch["src"]
        target_batchs = batch["trg"]
        # list
        input_lens = batch["src_len"]
        target_lens = batch["trg_len"]

        optimizer.zero_grad()

        loss = model(input_batchs, input_lens, target_batchs, target_lens, teacher_forcing_ratio)
        print_loss_total += loss.item()
        epoch_loss += loss.item()
        loss.backward()

        torch.nn.utils.clip_grad_norm_(model.parameters(), clip)

        optimizer.step()

        if print_every and (i + 1) % print_every == 0:
            print_loss_avg = print_loss_total / print_every
            print_loss_total = 0
            print('\tCurrent Loss: %.4f' % print_loss_avg)

    return epoch_loss / len(data_loader)


def evaluate(
        model,
        data_loader,
        print_every=None
):
    model.predict = False
    model.eval()
    if print_every == 0:
        print_every = 1

    print_loss_total = 0
    start = time.time()
    epoch_loss = 0
    with torch.no_grad():
        for i, batch in enumerate(data_loader):
            print(i)

            # shape = [seq_len, batch]
            input_batchs = batch["src"]
            target_batchs = batch["trg"]
            # list
            input_lens = batch["src_len"]
            target_lens = batch["trg_len"]

            loss = model(input_batchs, input_lens, target_batchs, target_lens, teacher_forcing_ratio=0)
            print_loss_total += loss.item()
            epoch_loss += loss.item()

            if print_every and (i + 1) % print_every == 0:
                print_loss_avg = print_loss_total / print_every
                print_loss_total = 0
                print('\tCurrent Loss: %.4f' % print_loss_avg)

    return epoch_loss / len(data_loader)


def translate(
        model,
        sample,
        idx2token=None
):
    model.predict = True
    model.eval()

    # shape = [seq_len, 1]
    input_batch = sample["src"]
    # list
    input_len = sample["src_len"]

    output_tokens = model(input_batch, input_len)
    output_tokens = [idx2token[t] for t in output_tokens]

    return "".join(output_tokens)

INPUT_DIM = len(before2id)
OUTPUT_DIM = len(after2id)

BATCH_SIZE = 32
ENC_EMB_DIM = 64
DEC_EMB_DIM = 64
HID_DIM = 64 #512
N_LAYERS = 2
ENC_DROPOUT = 0.5
DEC_DROPOUT = 0.5
LEARNING_RATE = 1e-4
N_EPOCHS = 2 #200
CLIP = 1

bidirectional = True
enc = Encoder(INPUT_DIM, ENC_EMB_DIM, HID_DIM, N_LAYERS, ENC_DROPOUT, bidirectional)
dec = Decoder(OUTPUT_DIM, DEC_EMB_DIM, HID_DIM, N_LAYERS, DEC_DROPOUT, bidirectional)
model = Seq2Seq(enc, dec, device, basic_dict=basic_dict).to(device)

optimizer = optim.Adam(model.parameters(), lr=LEARNING_RATE)
# optimizer_grouped_parameters = [
#         {'params': [p for n, p in model.named_parameters() if 'encoder' in n], 'lr': LEARNING_RATE},
#         {'params': [p for n, p in model.named_parameters() if 'decoder' in n], 'lr': LEARNING_RATE*2}
# ]
# optimizer = optim.Adam(optimizer_grouped_parameters)

train_set = TranslationDataset(before_num_data, after_num_data)#[0:200])
train_loader = DataLoader(train_set, batch_size=BATCH_SIZE, collate_fn=padding_batch)

best_valid_loss = float('inf')

for epoch in range(N_EPOCHS):

    start_time = time.time()
    train_loss = train(model, train_loader, optimizer, CLIP)
    valid_loss = evaluate(model, train_loader)
    end_time = time.time()

    if valid_loss < best_valid_loss:
        best_valid_loss = valid_loss
        torch.save(model.state_dict(), 'reverse-model-' + str(epoch) + '.pt')

    if epoch % 2 == 0:
        epoch_mins, epoch_secs = epoch_time(start_time, end_time)
        print(f'Epoch: {epoch+1:02} | Time: {epoch_mins}m {epoch_secs}s')
        print(f'\tTrain Loss: {train_loss:.3f} | Val. Loss: {valid_loss:.3f}')
