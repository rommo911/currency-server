FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py VERSION ./
COPY templates templates
COPY static/style.css static/style.css
# Non-root user; /data (volume) holds data.json, its .bak and the flags
RUN useradd -r -u 1000 app && mkdir /data && chown app /data
USER app
VOLUME /data
CMD ["python", "app.py"]
