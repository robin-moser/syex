FROM python:3.12-alpine

LABEL maintainer="Robin Moser"
WORKDIR /app

RUN pip install poetry==2.0.1

COPY pyproject.toml /app/
COPY poetry.lock /app/

RUN poetry install --no-cache --no-interaction --no-ansi

COPY . /app

CMD ["poetry", "run", "python", "/app/app.py"]
