from pathlib import Path
import numpy as np
import math
from itertools import groupby
import h5py
import numpy as np
import unicodedata
import cv2
import torch
from torch import nn
from torchvision.models import resnet50, resnet101
from torch.autograd import Variable
import torchvision
from data import preproc as pp
from data import evaluation
from torch.utils.data import Dataset
import time


import torch
import clip
from PIL import Image

from torch.utils.data.distributed import DistributedSampler
torch.distributed.init_process_group('nccl')
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--local_rank", default=0, type=int)
parser.add_argument("--name", default="default", type=str)
args = parser.parse_args()



class PositionalEncoding(nn.Module):
    def __init__(self, d_model, dropout=0.1, max_len=128):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)

        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer('pe', pe)

    def forward(self, x):
        x = x + self.pe[:x.size(0), :]
        return self.dropout(x)


class OCR(nn.Module):

    def __init__(self, vocab_len, hidden_dim, nheads,
                 num_encoder_layers, num_decoder_layers):
        super().__init__()

        # create ResNet-101 self.backbone
        self.backbone,_ = clip.load("ViT-B/16", device='cpu')
        self.backbone = self.backbone.visual
        
#         for name,p in self.backbone.named_parameters():
#             if "resblocks.10" not in name and  "resblocks.11" not in name:
#                 p.requires_grad =  False

            

        decoder_layer = nn.TransformerDecoderLayer(d_model=hidden_dim, nhead=nheads, activation="gelu")
        self.transformer_decoder = nn.TransformerDecoder(decoder_layer, num_layers=num_decoder_layers)
        self.converter = nn.Linear(768,hidden_dim)
        self.gelu = nn.GELU()

        # prediction heads with length of vocab
        # DETR used basic 3 layer MLP for output
        self.vocab = nn.Linear(hidden_dim,vocab_len)

        # output positional encodings (object queries)
        self.decoder = nn.Embedding(vocab_len, hidden_dim)
        self.query_pos = PositionalEncoding(hidden_dim, .2)
        self.dropout = nn.Dropout(.5)
        # spatial positional encodings, sine positional encoding can be used.
        # Detr baseline uses sine positional encoding.
        self.trg_mask = None
  
    def generate_square_subsequent_mask(self, sz):
        mask = torch.triu(torch.ones(sz, sz), 1)
        mask = mask.masked_fill(mask==1, float('-inf'))
        return mask

#     def get_feature(self,x):

#         x = self.backbone.visual.conv1(x)  # shape = [*, width, grid, grid]
#         x = x.reshape(x.shape[0], x.shape[1], -1)  # shape = [*, width, grid ** 2]
#         x = x.permute(0, 2, 1)  # shape = [*, grid ** 2, width]
#         x = torch.cat([self.backbone.visual.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device), x], dim=1)  # shape = [*, grid ** 2 + 1, width]
#         x = x + self.backbone.visual.positional_embedding.to(x.dtype)
#         x = self.backbone.visual.ln_pre(x)

#         x = x.permute(1, 0, 2)  # NLD -> LND
#         x = self.backbone.visual.transformer(x)
#         x = x.permute(1, 0, 2)  # LND -> NLD

#         x = self.backbone.visual.ln_post(x)  
#         return x
    def get_feature(self,x):

        x = self.backbone.conv1(x)  # shape = [*, width, grid, grid]
        x = x.reshape(x.shape[0], x.shape[1], -1)  # shape = [*, width, grid ** 2]
        x = x.permute(0, 2, 1)  # shape = [*, grid ** 2, width]
        x = torch.cat([self.backbone.class_embedding.to(x.dtype) + torch.zeros(x.shape[0], 1, x.shape[-1], dtype=x.dtype, device=x.device), x], dim=1)  # shape = [*, grid ** 2 + 1, width]
        x = x + self.backbone.positional_embedding.to(x.dtype)
        x = self.backbone.ln_pre(x)

        x = x.permute(1, 0, 2)  # NLD -> LND
        x = self.backbone.transformer(x)
        x = x.permute(1, 0, 2)  # LND -> NLD

        x = self.backbone.ln_post(x)  
        return x

    
    def make_len_mask(self, inp):
        return (inp == 0).transpose(0, 1)


    def forward(self, inputs, trg):
        # propagate inputs through ResNet-101 up to avg-pool layer
        h = self.get_feature(inputs)
        h = self.converter(h)
        h = self.gelu(h)
        h = self.dropout(h)
        # generating subsequent mask for target
        if self.trg_mask is None or self.trg_mask.size(0) != len(trg):
            self.trg_mask = self.generate_square_subsequent_mask(trg.shape[1]).to(trg.device)

        # Padding mask
        trg_pad_mask = self.make_len_mask(trg)

        # Getting postional encoding for target
        trg = self.decoder(trg)
        trg = self.query_pos(trg)
        
        output = self.transformer_decoder(trg.permute(1,0,2), h.permute(1,0,2), tgt_mask=self.trg_mask, 
                                  tgt_key_padding_mask=trg_pad_mask.permute(1,0))

        return self.vocab(output.transpose(0,1))


def make_model(vocab_len, hidden_dim=256, nheads=4,
                 num_encoder_layers=4, num_decoder_layers=4):
    
    return OCR(vocab_len, hidden_dim, nheads,
                 num_encoder_layers, num_decoder_layers)

"""
Uses generator functions to supply train/test with data.
Image renderings and text are created on the fly each time.
"""

class DataGenerator(Dataset):
    """Generator class with data streaming"""

    def __init__(self, source, split, transform, tokenizer):
        self.tokenizer = tokenizer
        self.transform = transform
        
        self.split = split
        self.dataset = dict()

        with h5py.File(source, "r") as f:
            self.dataset[self.split] = dict()

            self.dataset[self.split]['dt'] = np.array(f[self.split]['dt'])
            self.dataset[self.split]['gt'] = np.array(f[self.split]['gt'])
          
            randomize = np.arange(len(self.dataset[self.split]['gt']))
            np.random.seed(42)
            np.random.shuffle(randomize)

            self.dataset[self.split]['dt'] = self.dataset[self.split]['dt'][randomize]
            self.dataset[self.split]['gt'] = self.dataset[self.split]['gt'][randomize]

            # decode sentences from byte
            self.dataset[self.split]['gt'] = [x.decode() for x in self.dataset[self.split]['gt']]
            
        self.size = len(self.dataset[self.split]['gt'])


    def __getitem__(self, i):
        img = self.dataset[self.split]['dt'][i]
        
        #making image compatible with resnet
        img = np.repeat(img[..., np.newaxis],3, -1)    
#         img = pp.normalization(img)
            
        img = Image.fromarray(img)
        if self.transform is not None:
            img = self.transform(img)

        y_train = self.tokenizer.encode(self.dataset[self.split]['gt'][i]) 
        
        #padding till max length
        y_train = np.pad(y_train, (0, self.tokenizer.maxlen - len(y_train)))

        gt = torch.Tensor(y_train)

        return img, gt          

    def __len__(self):
      return self.size

class Tokenizer():
    """Manager tokens functions and charset/dictionary properties"""

    def __init__(self, chars, max_text_length=128):
        self.PAD_TK, self.UNK_TK,self.SOS,self.EOS = "¶", "¤", "SOS", "EOS"
        self.chars = [self.PAD_TK] + [self.UNK_TK ]+ [self.SOS] + [self.EOS] +list(chars)
        self.PAD = self.chars.index(self.PAD_TK)
        self.UNK = self.chars.index(self.UNK_TK)

        self.vocab_size = len(self.chars)
        self.maxlen = max_text_length

    def encode(self, text):
        """Encode text to vector"""

        text = unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode("ASCII").lower()
        text = " ".join(text.split())

        groups = ["".join(group) for _, group in groupby(text)]
        text = "".join([self.UNK_TK.join(list(x)) if len(x) > 1 else x for x in groups])
        encoded = []

        text = ['SOS'] + list(text) + ['EOS']
        for item in text:
            index = self.chars.index(item)
            index = self.UNK if index == -1 else index
            encoded.append(index)

        return np.asarray(encoded)

    def decode(self, text):
        """Decode vector to text"""
        
        decoded = "".join([self.chars[int(x)] for x in text if x > -1])
        decoded = self.remove_tokens(decoded)
        decoded = pp.text_standardize(decoded)

        return decoded

    def remove_tokens(self, text):
        """Remove tokens (PAD) from text"""

        return text.replace(self.PAD_TK, "").replace(self.UNK_TK, "")

import os
import datetime
import string

batch_size = 16
epochs = 200

# define paths
#change paths accordingly
source = 'iam'
source_path = '../data/{}.hdf5'.format(source)
output_path = os.path.join("..", "output", source)
target_path = os.path.join(output_path, "checkpoint_weights_iam_small_clip.hdf5")
os.makedirs(output_path, exist_ok=True)

# define input size, number max of chars per line and list of valid chars
input_size = (1024, 128, 1)
max_text_length = 128
charset_base = string.printable[:36].lower() + string.printable[36+26:95].lower() 

# charset_base = string.printable[:95]

print("source:", source_path)
print("output", output_path)
print("target", target_path)
print("charset:", charset_base)

from data.augmentation import build_data_aug

tokenizer = Tokenizer(charset_base)

import torchvision.transforms as T
local_rank = args.local_rank
device = torch.device("cuda:{}".format(local_rank))

train_trans = build_data_aug((224, 224), "train", resnet=1, )
valid_trans = build_data_aug((224, 224), "valid", resnet=1, )

train_sample = DistributedSampler(DataGenerator(source_path,'train',train_trans, tokenizer))
valid_sample = DistributedSampler(DataGenerator(source_path,'valid',valid_trans, tokenizer))
# valid_sample = DistributedSampler(DataGenerator(source_path,charset_base,max_text_length,'valid',transform))

train_loader = torch.utils.data.DataLoader(DataGenerator(source_path,'train',train_trans, tokenizer), batch_size=batch_size, sampler=train_sample, num_workers=6)
val_loader = torch.utils.data.DataLoader(DataGenerator(source_path,'valid',valid_trans, tokenizer), batch_size=batch_size,sampler=valid_sample, num_workers=6)


ddp_model = make_model( vocab_len=tokenizer.vocab_size,hidden_dim=256, nheads=4,
                 num_encoder_layers=4, num_decoder_layers=4)
for name, parameter in ddp_model.named_parameters():
    if "backbone" not in name and (parameter.dim()>1):
        nn.init.xavier_uniform_(parameter)

ddp_model = ddp_model.to(device)
ddp_model = nn.SyncBatchNorm.convert_sync_batchnorm(ddp_model)
model = nn.parallel.DistributedDataParallel(ddp_model, device_ids=[local_rank], output_device=local_rank,find_unused_parameters=True)


# for name, parameter in model.named_parameters():
#     if "backbone" not in name and (parameter.dim()>1):
#         nn.init.xavier_uniform_(parameter)


class LabelSmoothing(nn.Module):
    "Implement label smoothing."
    def __init__(self, size, padding_idx=0, smoothing=0.0):
        super(LabelSmoothing, self).__init__()
        self.criterion = nn.KLDivLoss(size_average=False)
        self.padding_idx = padding_idx
        self.confidence = 1.0 - smoothing
        self.smoothing = smoothing
        self.size = size
        self.true_dist = None
        
    def forward(self, x, target):
        assert x.size(1) == self.size
        true_dist = x.data.clone()
        true_dist.fill_(self.smoothing / (self.size - 2))
        true_dist.scatter_(1, target.data.unsqueeze(1), self.confidence)
        true_dist[:, self.padding_idx] = 0
        mask = torch.nonzero(target.data == self.padding_idx)
        if mask.dim() > 0:
            true_dist.index_fill_(0, mask.squeeze(), 0.0)
        self.true_dist = true_dist
        return self.criterion(x, Variable(true_dist, requires_grad=False))


criterion = LabelSmoothing(size=tokenizer.vocab_size, padding_idx=0, smoothing=0.1)
criterion.to(device)
lr = .0002 # learning rate
param_dicts = [
    {"params": [p for n, p in model.named_parameters() if "backbone" not in n and p.requires_grad]},
    {
        "params": [p for n, p in model.named_parameters() if "backbone" in n and p.requires_grad],
        "lr": .00002,
    },
]

optimizer = torch.optim.AdamW(param_dicts, lr=lr,weight_decay=.0004)
lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, 1, gamma=0.85)


def train(model, criterion, optimiser,dataloader):
 
    model.train()
    total_loss = 0
    for batch, (imgs, labels_y,) in enumerate(dataloader):
          imgs = imgs.to(device)
          labels_y = labels_y.to(device)
    
          optimiser.zero_grad()
          output = model(imgs.float(),labels_y.long()[:,:-1])
 
          norm = (labels_y != 0).sum()
          loss = criterion(output.log_softmax(-1).contiguous().view(-1, tokenizer.vocab_size), labels_y[:,1:].contiguous().view(-1).long()) / norm
 
          loss.backward()
          torch.nn.utils.clip_grad_norm_(model.parameters(), 0.2)
          optimizer.step()
          total_loss += loss.item() * norm
 
    return total_loss / len(dataloader)
 
def evaluate(model, criterion, dataloader,):
 
    model.eval()
    epoch_loss = 0
 
    with torch.no_grad():
      for batch, (imgs, labels_y,) in enumerate(dataloader):
            imgs = imgs.to(device)
            labels_y = labels_y.to(device)
 
            output = model(imgs.float(),labels_y.long()[:,:-1])
              
            norm = (labels_y != 0).sum()
            loss = criterion(output.log_softmax(-1).contiguous().view(-1, tokenizer.vocab_size), labels_y[:,1:].contiguous().view(-1).long()) / norm
  
            epoch_loss += loss.item() * norm
 
    return epoch_loss / len(dataloader)

#train model
import torch.distributed as dist
# dist.init_process_group(backend='nccl', init_method='env://')
def epoch_time(start_time, end_time):
    elapsed_time = end_time - start_time
    elapsed_mins = int(elapsed_time / 60)
    elapsed_secs = int(elapsed_time - (elapsed_mins * 60))
    return elapsed_mins, elapsed_secs
 

best_valid_loss = np.inf
c = 0
for epoch in range(200):
#     print(f'Local rank: {local_rank}',f'Epoch: {epoch+1:02}','learning rate{}'.format(lr_scheduler.get_last_lr()))
 
    dist.barrier()

    start_time = time.time()
     
 
    train_loss = train(model,  criterion, optimizer, train_loader)
    valid_loss = evaluate(model, criterion, val_loader)
    
    epoch_mins, epoch_secs = epoch_time(start_time, time.time())
    c+=1
    if valid_loss < best_valid_loss:
            print("Saving model with loss: ", valid_loss)
            best_valid_loss = valid_loss
            if local_rank == 0:
                torch.save(model.state_dict(), target_path)
            c=0
 
    if c>4:
        #decrease lr if loss does not deacrease after 5 steps
        lr_scheduler.step()
        c=0
    if local_rank == 0:
        print(f'Inside local rank: {local_rank}',f'Epoch: {epoch+1:02}','learning rate{}'.format(lr_scheduler.get_last_lr()))
        print(f'Time: {epoch_mins}m {epoch_secs}s') 
        print(f'Train Loss: {train_loss:.3f}')
        print(f'Val   Loss: {valid_loss:.3f}')




