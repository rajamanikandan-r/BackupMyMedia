# 1. Use an official Python image as the base
FROM python:3.11-slim

# 2. Set the directory inside the container where your code will live
WORKDIR /app

# 3. Copy the requirements file and install the libraries
# We do this before copying the rest of the code to speed up builds
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copy the rest of your application code
COPY . .

# 5. Tell the container to start the web server (Gunicorn)
# It will listen on the port provided by Google Cloud (usually 8080)
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 main:app