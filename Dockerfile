FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates templates
COPY static/style.css static/style.css
# Non-root user; /data (volume) holds data.json and flags
RUN useradd -r -u 1000 app && mkdir /data && chown app /data
USER app
ENV DATA_DIR=/data
VOLUME /data
CMD ["python", "app.py"]
