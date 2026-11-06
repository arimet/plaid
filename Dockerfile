FROM alpine:3.20
RUN apk add --no-cache python3 docker-cli docker-cli-compose
WORKDIR /app
COPY installer /app/installer
ENV ARR_STATE_DIR=/state PYTHONUNBUFFERED=1
EXPOSE 7000
LABEL io.arr-installer=1 org.opencontainers.image.source=https://github.com/arimet/plaid
ENTRYPOINT ["python3", "-m", "installer"]
