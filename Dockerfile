FROM public.ecr.aws/docker/library/python:3-alpine

RUN apk upgrade --no-cache

WORKDIR /app
COPY webhook.py .

ENV LISTEN_PORT=9999
EXPOSE 9999

CMD ["python3", "webhook.py"]
