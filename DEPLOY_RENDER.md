# Deploy do Custo Chef no Render

Este projeto foi preparado para o caminho mais simples de deploy pessoal: `Render Blueprint`.

## O que já está pronto

- `render.yaml` cria:
  - 1 web service
  - 1 banco Postgres
- `wsgi.py` expõe a aplicação para o `gunicorn`
- o app já lê:
  - `DATABASE_URL`
  - `SECRET_KEY`
  - `PORT`

## Como publicar

1. Crie uma conta no Render.
2. Suba este projeto para um repositório no GitHub.
3. No Render, escolha `New +` -> `Blueprint`.
4. Conecte o repositório.
5. O Render vai ler o arquivo `render.yaml`.
6. Confirme a criação do web service e do banco.
7. Aguarde o deploy terminar.
8. Abra a URL pública gerada pelo Render.

## Observações

- O primeiro carregamento pode demorar um pouco no plano gratuito.
- Como o banco é externo, o sistema não depende do VS Code aberto.
- Se quiser continuar usando localmente, o app ainda funciona sem `DATABASE_URL`, usando SQLite.

## Comando local

Para rodar localmente:

```powershell
python backend/app.py
```
