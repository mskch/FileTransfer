FROM python:3.12-slim
WORKDIR /app
RUN groupadd --gid 10001 sft && useradd --uid 10001 --gid 10001 --no-create-home sft
COPY secure_transfer /app/secure_transfer
USER 10001:10001
EXPOSE 8443
CMD ["python", "-m", "secure_transfer.server", "--config", "/config/server.json"]
