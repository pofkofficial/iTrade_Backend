# Use the official Python image as the base image
FROM python:3.10

# Set the working directory in the container
WORKDIR /app

# Copy the application files into the working directory
COPY . /app

# Install the application dependencies
RUN pip install -r requirements.txt

# Expose the port the app runs on
EXPOSE 8000

# Define the entry point for the container
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
