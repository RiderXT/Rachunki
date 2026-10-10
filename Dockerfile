FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/srv
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8501
CMD ["streamlit", "run", "app/dashboard.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
