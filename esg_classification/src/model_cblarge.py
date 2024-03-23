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
                          AutoTokenizer, CamembertForMaskedLM)

import gc


nlp = spacy.load('fr_core_news_md')

BATCH_SIZE = 4


DATASET_PATH = './data/esg_fr_classification.csv'
OUTPUT_MODEL_PATH = './models/test/pl_model_large.pt'
TB_LOGS_PATH = './Tensorboard_logs/test/cb-large'
ID_TO_LABEL = {
    0: 'non-esg',
    1: 'environnemental',
    2: 'social',
    3: 'gouvernance'}
LABEL_TO_ID = {v: k for k, v in ID_TO_LABEL.items()}
num_labels = len(ID_TO_LABEL)

def cleanse_french_text(text):
    text = text.lower()
    text = text.translate(str.maketrans('', '', string.punctuation))
    
    # perform lemmatisation
    doc = nlp(text)
    text = ' '.join([token.lemma_ for token in doc])
    
    # Remove stopwords
    words = text.split()
    cleaned_words = [word for word in words if word not in fr_stop]
    cleaned_text = ' '.join(cleaned_words)

    return cleaned_text


def tokenize_batch(samples, tokenizer):
    text = [sample["text"] for sample in samples]
    labels = torch.tensor([sample["label"] for sample in samples])
    # str_labels = [sample["esg_category"] for sample in samples]
    # The tokenizer handles tokenization, padding, truncation and attn mask

    tokens = tokenizer(text, padding="longest", return_tensors="pt", truncation=True)

    return {"input_ids": tokens.input_ids,
            "attention_mask": tokens.attention_mask,
            "labels": labels,
            # "str_labels": str_labels,
            "sentences": text}

class CustomTextDataset(Dataset):
    def __init__(self, dataframe, tokenizer, max_length=512):
        self.df = dataframe
        self.tokenizer = tokenizer
        self.max_length = max_length

    def __len__(self):
        return len(self.df)
    

    def __getitem__(self, idx):

        text = self.df.iloc[idx]['text'].tolist()
        label = self.df.iloc[idx]['label'].tolist()
        return {"text": text, "label": label}
    





print(f"Loading dataset")
# Read and preprocess your dataset
df = pd.read_csv(DATASET_PATH, encoding='utf-8', sep=',')
df = df.sample(frac=0.01, random_state=42).reset_index(drop=True)
df['text'].apply(cleanse_french_text)

df['label'] = df['esg_category'].map(LABEL_TO_ID)


TRAIN_SIZE = 0.6

print("Splitting dataset...")
# Split the DataFrame into train, validation, and test sets
train_df, temp_df = train_test_split(df, train_size=TRAIN_SIZE, stratify=df['label'], random_state=42)
val_df, test_df = train_test_split(temp_df, test_size=0.5, stratify=temp_df['label'], random_state=42)

# Initialize tokenizer
tokenizer = AutoTokenizer.from_pretrained('camembert/camembert-large')

# Create dataset instances
train_dataset = CustomTextDataset(train_df, tokenizer)
val_dataset = CustomTextDataset(val_df, tokenizer)
test_dataset = CustomTextDataset(test_df, tokenizer)

# Create DataLoaders
train_dataloader = DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True,
                              collate_fn=functools.partial(tokenize_batch, tokenizer=tokenizer))
val_dataloader = DataLoader(val_dataset, batch_size=BATCH_SIZE,
                            shuffle=False, collate_fn=functools.partial(tokenize_batch, tokenizer=tokenizer))
test_dataloader = DataLoader(test_dataset, batch_size=BATCH_SIZE, shuffle=False,
                             collate_fn=functools.partial(tokenize_batch, tokenizer=tokenizer))

gc.collect()
torch.cuda.empty_cache()

print("\n created dataloaders.\n ")

class LightningModel(pl.LightningModule):
    def __init__(self, model_name, num_labels, lr, weight_decay, from_scratch=False):
        super().__init__()
        self.save_hyperparameters()
        if from_scratch:
            # Si `from_scratch` est vrai, on charge uniquement la config (nombre de couches, hidden size, etc.) et pas les poids du modèle 
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
        self.num_labels = self.model.num_labels

    def forward(self, batch):
        return self.model(
            input_ids=batch["input_ids"],
            attention_mask=batch["attention_mask"]
        )

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
        
        
lightning_model = LightningModel("camembert/camembert-large", num_labels, lr=3e-5, weight_decay=0.)

print("\n created lightning model.\n ")
gc.collect()
torch.cuda.empty_cache()

# ----------------------------------------------------
# ----------------------------------------------------
# ------------------- TRAINING -----------------------
# ----------------------------------------------------
# ----------------------------------------------------

# time start
start = time.time()

model_checkpoint = pl.callbacks.ModelCheckpoint(monitor="valid/acc", mode="max")

logger = TensorBoardLogger(TB_LOGS_PATH, name="pl_model")


camembert_trainer = pl.Trainer(
    logger=logger,
    max_epochs=20,
    # accelerator="gpu",
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

gc.collect()
torch.cuda.empty_cache()



# ----------------------------------------------------
# ----------------------------------------------------
# ------------------- TESTING ------------------------
# ----------------------------------------------------
# ----------------------------------------------------


# ensure model in evaluation mode
lightning_model.model.eval()

test_dataloader = DataLoader(
    test_dataset, 
    batch_size=BATCH_SIZE, 
    shuffle=False, 
    collate_fn=functools.partial(tokenize_batch, tokenizer=tokenizer)
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

gc.collect()
torch.cuda.empty_cache()

with open('callbacks_plModel_large.txt', 'w') as file:
    file.write(callbacks_str)    
print("Callbacks saved to 'callbacks_plModel_large.txt'")