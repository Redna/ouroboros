#!/bin/bash
MODEL_URL="https://huggingface.co/bartowski/mistralai_Mistral-Small-3.2-24B-Instruct-2506-GGUF/resolve/main/mistralai_Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf"
JINJA_URL="https://huggingface.co/bartowski/mistralai_Mistral-Small-3.2-24B-Instruct-2506-GGUF/resolve/main/Mistral-Small-3.2-24B-Instruct-2506.jinja"

echo "Downloading mistralai_Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf..."
wget -c "$MODEL_URL" -O models/mistralai_Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf

echo "Downloading Mistral-Small-3.2-24B-Instruct-2506.jinja..."
wget -c "$JINJA_URL" -O models/Mistral-Small-3.2-24B-Instruct-2506.jinja

echo "Done!"
