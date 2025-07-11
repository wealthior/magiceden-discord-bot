FROM python:3.10

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8080

ENV PYTHONUNBUFFERED True

CMD ["python", "magiceden_bot.py"]