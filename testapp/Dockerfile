FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
EXPOSE 8000
# Exec form, so uvicorn is PID 1 and receives the container's SIGTERM itself.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
