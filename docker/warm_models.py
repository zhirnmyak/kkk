"""Preload the actual search and Russian/English OCR models into the image."""
from sentence_transformers import SentenceTransformer
import easyocr

name = 'intfloat/multilingual-e5-small'
try:
    model = SentenceTransformer(name, local_files_only=True)
except OSError:
    model = SentenceTransformer(name)
vector = model.encode(['passage: Проверка поиска по учебным материалам.'], normalize_embeddings=True)
assert vector.shape == (1, 384), vector.shape
reader = easyocr.Reader(['ru', 'en'], gpu=False, verbose=False)
print('Search model and Russian/English OCR are ready.', flush=True)

