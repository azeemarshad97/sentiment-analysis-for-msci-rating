import functools
import string
import time
from pprint import pprint

import matplotlib.pyplot as plt
import pandas as pd
import pytorch_lightning as pl
import spacy
import torch
import torch.nn.functional as F
from datasets import Dataset, load_dataset
from pytorch_lightning.loggers import TensorBoardLogger
from sklearn.metrics import confusion_matrix, f1_score
from sklearn.model_selection import train_test_split
from spacy.lang.fr.stop_words import STOP_WORDS as fr_stop
from torch.optim.lr_scheduler import StepLR
from torch.utils.data import DataLoader
from tqdm.notebook import tqdm
from transformers import (AutoConfig, AutoModelForSequenceClassification,
                          AutoTokenizer, CamembertForMaskedLM, AutoModel)

import utils

# import nn
from torch import nn


nlp = spacy.load('fr_core_news_md')


DATASET_PATH = '~/thesis/data/esg_fr_classification.csv'
OUTPUT_MODEL_PATH = '~/thesis/models/minibatch_model.pt'
TB_LOGS_PATH = '~/thesis/Tensorboard_logs/test/pl_minibatching_model'
BATCH_SIZE = 8
TRAIN_SIZE = 0.6

ID_TO_LABEL = {
    0: 'non-esg',
    1: 'environnemental',
    2: 'social',
    3: 'gouvernance'
}

LABEL_TO_ID = {v: k for k, v in ID_TO_LABEL.items()}

df = pd.read_csv(DATASET_PATH, encoding='utf-8', sep=',')
df = df.sample(frac=1, random_state=42).reset_index(drop=True)
df['text'].apply(utils.cleanse_french_text)
df['label'] = df['esg_category'].apply(lambda x: LABEL_TO_ID[x])
print(df['esg_category'].value_counts())


tokenizer = AutoTokenizer.from_pretrained('camembert-base')

df = utils.prepare_dataset(df, tokenizer=tokenizer, stride = 250)


# train, test and validation split
df_train, df_temp = train_test_split(df, train_size=TRAIN_SIZE, stratify=df.label, random_state=42)
df_val, df_test = train_test_split(df_temp, test_size=0.5, stratify=df_temp.label, random_state=42)

print(f"train size: {df_train.shape} \t test size: {df_test.shape} \t validation size: {df_val.shape}")

df_train["len"] = df_train["text"].apply(lambda x: len(x))


print(df_train["len"].describe())

# Convert df to Hugging Face Datasets
train_dataset = Dataset.from_pandas(df_train)
test_dataset = Dataset.from_pandas(df_test)
val_dataset = Dataset.from_pandas(df_val)

val_dataloader = DataLoader(val_dataset, collate_fn=functools.partial(utils.tokenize_batch_bis, tokenizer=tokenizer), batch_size=16)
print(next(iter(val_dataloader))['input_ids'])
print(next(iter(val_dataloader))['attention_mask'])
print(next(iter(val_dataloader))['labels'])

num_labels = len(df_train["label"].unique())
train_dataloader = DataLoader(
    train_dataset, 
    batch_size=BATCH_SIZE, 
    shuffle=True,
    collate_fn=functools.partial(utils.tokenize_batch_bis, tokenizer=tokenizer)
)
val_dataloader = DataLoader(
    val_dataset, 
    batch_size=BATCH_SIZE, 
    shuffle=False, 
    collate_fn=functools.partial(utils.tokenize_batch_bis, tokenizer=tokenizer)
)


class LightningModel(pl.LightningModule):
    def __init__(self, model_name, num_labels, lr, weight_decay, id_to_label, from_scratch=False):
        super().__init__()
        self.save_hyperparameters()
        if from_scratch:
            # Si `from_scratch` est vrai, on charge uniquement la config (nombre de couches, hidden size, etc.)
            # et pas les poids du modèle 
            config = AutoConfig.from_pretrained(
                model_name, num_labels=num_labels
            )
            self.model = AutoModelForSequenceClassification.from_config(config)
        else:
            # Cette méthode permet de télécharger le bon modèle pré-entraîné directement depuis HGF
            self.model = AutoModelForSequenceClassification.from_pretrained(
                model_name, num_labels=num_labels
            )
            
        self.lr = lr
        self.weight_decay = weight_decay
        self.num_labels = num_labels
        self.id_to_label = id_to_label


    def forward(self, batch):
        # Original model output
        outputs = self.model(
            input_ids=batch["input_ids"],
            attention_mask=batch["attention_mask"]
        )
        return outputs
        
        

    def training_step(self, batch):
        out = self.forward(batch)

        logits = out.logits
        # -------- MASKED --------
        loss_fn = torch.nn.CrossEntropyLoss()
        loss = loss_fn(logits.view(-1, self.num_labels), batch["labels"].view(-1))

        # ------ END MASKED ------

        self.log("train/loss", loss,  logger=True, on_epoch=True, on_step=True)

        return loss

    def validation_step(self, batch, batch_index):
        labels = batch["labels"]
        out = self.forward(batch)

        preds = torch.max(out.logits, -1).indices
        # -------- MASKED --------
        acc = (batch["labels"] == preds).float().mean()
        # ------ END MASKED ------
        self.log("valid/acc", acc, logger=True, on_epoch=True, on_step=True)

        f1 = f1_score(batch["labels"].cpu().tolist(), preds.cpu().tolist(), average="macro")
        self.log("valid/f1", f1, logger=True, on_epoch=True, on_step=True)

    def predict_step(self, batch, batch_idx):
        """La fonction predict step facilite la prédiction de données. Elle est 
        similaire à `validation_step`, sans le calcul des métriques.
        """
        out = self.forward(batch)

        return torch.max(out.logits, -1).indices

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.model.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        scheduler = StepLR(optimizer, step_size=2, gamma=0.1)

        return {
            "optimizer": optimizer,
            "lr_scheduler": {"scheduler": scheduler, "interval": "epoch"}, 
            "gradient_clip_val": 1.0,  
        }
        


      
lightning_model = LightningModel(
    "camembert-base",
    num_labels,
    id_to_label=ID_TO_LABEL,
    lr=0.00003,
    weight_decay=0.00002)

print("\n created lightning model.\n ")


# ----------------------------------------------------
# ----------------------------------------------------
# ------------------- TRAINING -----------------------
# ----------------------------------------------------
# ----------------------------------------------------

print(f"\n\n\n\n TRAINING \n\n\n\n")


# time start
start = time.time()

model_checkpoint = pl.callbacks.ModelCheckpoint(monitor="valid/acc", mode="max")

logger = TensorBoardLogger(TB_LOGS_PATH, name="pl_model")


camembert_trainer = pl.Trainer(
    logger=logger,
    max_epochs=20,
    accelerator="gpu",
    accumulate_grad_batches=5,
    callbacks=[
        pl.callbacks.EarlyStopping(monitor="valid/acc", patience=4, mode="max"),
        model_checkpoint,
        
    ]
)


camembert_trainer.fit(lightning_model, train_dataloaders=train_dataloader, val_dataloaders=val_dataloader) 

# After model training, save callback details
early_stopping_callback = str(pl.callbacks.EarlyStopping(monitor="valid/acc", patience=4, mode="max"))
model_checkpoint_callback = str(model_checkpoint)

callbacks_str = f"Early Stopping Callback:\n{early_stopping_callback}\n\nModel Checkpoint Callback:\n{model_checkpoint_callback}"

# end time
end = time.time()
total_time = end - start
print(f"Training took {total_time} seconds")
    
lightning_model.model.save_pretrained(OUTPUT_MODEL_PATH)


# 
# TESTING
# 
print(F"\n\n\n\n TESTING \n\n\n\n")


# ensure model in evaluation mode
lightning_model.model.eval()

test_dataloader = DataLoader(
    test_dataset, 
    batch_size=BATCH_SIZE, 
    shuffle=False, 
    collate_fn=functools.partial(utils.tokenize_batch_bis, tokenizer=tokenizer)
)

# store predictions and true labels
test_preds = []
test_labels = []

# Disable gradient calculations for evaluation
with torch.no_grad():
    for batch in tqdm(test_dataloader):
        # Make predictions
        outputs = lightning_model.model(
            input_ids=batch['input_ids'], 
            attention_mask=batch['attention_mask']
        )

        # Get the predictions and true labels
        logits = outputs.logits
        predictions = torch.argmax(logits, dim=-1)
        test_preds.extend(predictions.tolist())
        test_labels.extend(batch['labels'].tolist())

# computing metrics
accuracy = sum(1 for x, y in zip(test_preds, test_labels) if x == y) / len(test_labels)
f1 = f1_score(test_labels, test_preds, average='macro')

print(f"Test Accuracy: {accuracy:.4f}")
print(f"Test F1 Score: {f1:.4f}")


# confusion matrix
label_names = list(ID_TO_LABEL.values())
utils.plot_confusion_matrix(test_labels, test_preds, label_names=label_names, image_path = './confusion_matrix.png')