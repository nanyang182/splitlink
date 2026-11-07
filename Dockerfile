FROM python:3.12-slim

WORKDIR /app
COPY . .

# 镜像里默认监听所有网卡，否则宿主机访问不到
ENV SPLITLINK_HOST=0.0.0.0 \
    SPLITLINK_PORT=8787 \
    PYTHONUNBUFFERED=1

EXPOSE 8787
CMD ["python", "app.py"]
