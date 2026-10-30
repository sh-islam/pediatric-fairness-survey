FROM python:3.12-slim
WORKDIR /app
COPY app.py .
ENV DATA_DIR=/data PORT=8096 PYTHONUNBUFFERED=1
CMD ["python", "app.py"]
