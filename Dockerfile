FROM python:3-alpine

WORKDIR /app
COPY webhook.py .

ENV LISTEN_PORT=9999
EXPOSE 9999

CMD ["python3", "webhook.py"]
