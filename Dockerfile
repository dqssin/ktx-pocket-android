FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 KTX_DATA_DIR=/data
WORKDIR /app
COPY webapp/requirements.txt webapp/requirements.txt
COPY webapp/vendor webapp/vendor
RUN echo '198c89042a56cbcbdc86e970a58b613b928dc1922b80f4a2c97a168f0bd7d49c  webapp/vendor/pykorail-0.5.1-py3-none-any.whl' | sha256sum -c - \
    && pip install --no-cache-dir -r webapp/requirements.txt \
    && useradd --uid 10001 --create-home ktx \
    && mkdir /data && chown ktx:ktx /data
COPY app/src/main/python app/src/main/python
COPY webapp webapp
COPY tests tests
USER ktx
EXPOSE 8080
HEALTHCHECK --interval=20s --timeout=3s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"
CMD ["python", "-m", "webapp"]
