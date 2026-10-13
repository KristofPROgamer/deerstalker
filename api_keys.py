"""Optional cloud-provider keys, supplied through environment variables."""
import os

google_apikeys = [key.strip() for key in os.getenv("GOOGLE_API_KEYS", "").split(",") if key.strip()]
ollama_apikeys = [key.strip() for key in os.getenv("OLLAMA_API_KEYS", "").split(",") if key.strip()]
