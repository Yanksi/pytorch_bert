import time
from datetime import datetime
from pathlib import Path

import torch

from torch import nn
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from bert.dataset import IMDBBertDataset
from bert.model import BERT

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def percentage(batch_size: int, max_len: int, current: int):
    batched_max = max_len // batch_size
    return round(current / batched_max * 100, 2)


def nsp_accuracy(result: torch.Tensor, target: torch.Tensor):
    s = (result.argmax(1) == target.argmax(1)).sum()
    return round(float(s / result.size(0)), 2)


def token_accuracy(result: torch.Tensor, target: torch.Tensor, mask: torch.Tensor):
    r = result.argmax(-1).masked_select(~mask)
    t = target.masked_select(~mask)
    s = (r == t).sum()
    return round(float(s / (result.size(0) * result.size(1))), 2)


class BertTrainer:

    def __init__(self, model: BERT, dataset: IMDBBertDataset,
                 log_dir: Path,
                 checkpoint_dir: Path = None,
                 print_progress_every: int = 10,
                 print_accuracy_every: int = 50,
                 batch_size: int = 24,
                 learning_rate: float = 0.005,
                 epochs: int = 5,
                 ):
        self.model = model
        self.dataset = dataset

        self.batch_size = batch_size
        self.loader = DataLoader(self.dataset, batch_size=self.batch_size, shuffle=True)

        self.writer = SummaryWriter(str(log_dir))

        self.checkpoint_dir = checkpoint_dir

        self.criterion = nn.BCEWithLogitsLoss().to(device)
        self.ml_criterion = nn.NLLLoss(ignore_index=0).to(device)
        self.optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=0.015)

        self.epochs = epochs

        self.current_epoch = 0

        self._splitter_size = 35

        self._ds_len = len(self.dataset)
        self._batched_len = self._ds_len // self.batch_size

        self._print_every = print_progress_every
        self._accuracy_every = print_accuracy_every

    def print_summary(self):
        ds_len = len(self.dataset)

        print("Model Summary\n")
        print('=' * self._splitter_size)
        print(f"Device: {device}")
        print(f"Training dataset len: {ds_len}")
        print(f"Max / Optimal sentence len: {self.dataset.optimal_sentence_length}")
        print(f"Vocab size: {len(self.dataset.vocab)}")
        print(f"Batch size: {self.batch_size}")
        print(f"Batched dataset len: {self._batched_len}")
        print('=' * self._splitter_size)
        print()

    def __call__(self):
        for self.current_epoch in range(self.current_epoch, self.epochs):
            loss = self.train(self.current_epoch)
            self.save_checkpoint(self.current_epoch, step=-1, loss=loss)

    def train(self, epoch: int):
        print(f"Begin epoch {epoch}")

        prev = time.time()
        average_nsp_loss = 0
        average_mlm_loss = 0
        for i, value in enumerate(self.loader):
            index = i + 1

            self.optimizer.zero_grad()

            inp, mask, token_mask, mask_target, nsp_target = value
            token, nsp = self.model(inp, mask)

            tm = token_mask.unsqueeze(-1).expand_as(token)
            token = token.masked_fill(tm, 0)

            loss_token = self.ml_criterion(token.transpose(1, 2), mask_target)  # 1D tensor as target is required
            loss_nsp = self.criterion(nsp, nsp_target)  # 1D tensor as target is required

            loss = loss_token + loss_nsp
            average_nsp_loss += loss_nsp
            average_mlm_loss += loss_token

            loss.backward()
            self.optimizer.step()

            if index % self._print_every == 0:
                elapsed = time.gmtime(time.time() - prev)
                passed = percentage(self.batch_size, self._ds_len, index)
                global_step = epoch * len(self.loader) + index

                print_nsp_loss = average_nsp_loss / self._print_every
                print_mlm_loss = average_mlm_loss / self._print_every

                s = f"{time.strftime('%H:%M:%S', elapsed)}"
                s += f" | Epoch {epoch + 1} | {index} / {self._batched_len} ({passed}%) | " \
                     f"NSP loss {print_nsp_loss:6.2f} | MLM loss {print_mlm_loss:6.2f}"

                if index % self._accuracy_every == 0:
                    nsp_acc = nsp_accuracy(nsp, nsp_target)
                    token_acc = token_accuracy(token, mask_target, token_mask)
                    s += f" | NSP accuracy {nsp_acc} | " \
                         f"Token accuracy {token_acc}"
                    self.writer.add_scalar("NSP train accuracy", nsp_acc, global_step=global_step)
                    self.writer.add_scalar("Token train accuracy", token_acc, global_step=global_step)

                print(s)

                self.writer.add_scalar("NSP loss", print_nsp_loss, global_step=global_step)
                self.writer.add_scalar("MLM loss", print_mlm_loss, global_step=global_step)

                average_nsp_loss = 0
                average_mlm_loss = 0
        return loss

    def save_checkpoint(self, epoch, step, loss):
        if not self.checkpoint_dir:
            return

        prev = time.time()
        name = f"bert_epoch{epoch}_step{step}_{datetime.utcnow().timestamp():.0f}.pt"

        torch.save({
            'epoch': epoch,
            'model_state_dict': self.model.state_dict(),
            'optimizer_state_dict': self.optimizer.state_dict(),
            'loss': loss,
        }, self.checkpoint_dir.joinpath(name))

        print()
        print('=' * self._splitter_size)
        print(f"Model saved as '{name}' for {time.time() - prev:.2f}s")
        print('=' * self._splitter_size)
        print()

    def load_checkpoint(self, path: Path):
        print('=' * self._splitter_size)
        print(f"Restoring model {path}")
        checkpoint = torch.load(path)
        self.current_epoch = checkpoint['epoch']
        self.model.load_state_dict(checkpoint['model_state_dict'])
        self.optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        print("Model is restored.")
        print('=' * self._splitter_size)
