FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt pyproject.toml LICENSE THIRD_PARTY_LICENSES ./
RUN pip install -r requirements.txt
COPY src ./src
RUN pip install --no-deps .
ENTRYPOINT ["jmstats"]
CMD ["update"]
