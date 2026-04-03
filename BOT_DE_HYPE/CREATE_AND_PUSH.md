# Crear y subir `BOT_DE_HYPE` como repositorio independiente

Ya tienes los archivos creados dentro de esta carpeta:

- `bot_hype.py`
- `README.md`
- `requirements.txt`

## Opción rápida (GitHub CLI)

```bash
cd BOT_DE_HYPE
git init
git add .
git commit -m "Initial commit: BOT_DE_HYPE strategy bot"
gh repo create BOT_DE_HYPE --public --source=. --remote=origin --push
```

## Opción manual (sin GitHub CLI)

1. Crea en GitHub un repositorio vacío llamado `BOT_DE_HYPE`.
2. Luego ejecuta:

```bash
cd BOT_DE_HYPE
git init
git add .
git commit -m "Initial commit: BOT_DE_HYPE strategy bot"
git branch -M main
git remote add origin https://github.com/TU_USUARIO/BOT_DE_HYPE.git
git push -u origin main
```
