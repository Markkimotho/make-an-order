# make-an-order

**Deployed URL**: [Make-An-Order App](https://make-an-order-ce858f58b429.herokuapp.com/)

## Table of Contents

- [make-an-order](#make-an-order)
  - [Table of Contents](#table-of-contents)
  - [Overview](#overview)
  - [Features](#features)
  - [Tech Stack](#tech-stack)
  - [Getting Started](#getting-started)
    - [Prerequisites](#prerequisites)
    - [Installation](#installation)
      - [Mandatory Step if you want to implement OAuth2.0 with Google](#mandatory-step-if-you-want-to-implement-oauth20-with-google)
  - [Usage](#usage)
  - [API Documentation](#api-documentation)
    - [Authentication Endpoints](#authentication-endpoints)
    - [Customer Endpoints](#customer-endpoints)
    - [Order Endpoints](#order-endpoints)
  - [Testing](#testing)
    - [Test Coverage](#test-coverage)
  - [Environment Variables](#environment-variables)
  - [Deployment](#deployment)
    - [CI/CD with GitHub Actions](#cicd-with-github-actions)
      - [Steps to Set Up CI/CD with GitHub Actions](#steps-to-set-up-cicd-with-github-actions)
    - [Steps to Deploy on Heroku](#steps-to-deploy-on-heroku)
  - [Project Structure](#project-structure)

## Overview

**make-an-order** is an simple RESTful service for managing customers and orders implemented using Python.
It implements secure authentication via Google OAuth 2.0.
It implements sending of SMS notifications to the customer through Africa's Talking API when an order is placed

The app provides:

- [A web interface for managing orders and customers.](https://make-an-order-ce858f58b429.herokuapp.com/)
- RESTful API endpoints for programmatic access.
- Integration with SMS for real-time notifications.

## Features

- **User Authentication**:
  - Secure login with Google OAuth 2.0.
- **Customer Management**:
  - Add, update, view, and delete customer records.
- **Order Management**:
  - Create, update, view, and delete orders.
- **SMS Notifications**:
  - Automatically notify customers when they make an order via SMS.
- **Database Integration**:
  - Stores data using MySQL (powered by JawsDB on Heroku).
- **Comprehensive API**:
  - RESTful API design for easy integrations.
- **Testing Support**:
  - Automated tests using Pytest to ensure system reliability.
  
## Tech Stack

- **Backend**: Flask, SQLAlchemy
- **Database**: MySQL (via JawsDB)
- **Authentication**: Google OAuth 2.0
- **Messaging**: Africa's Talking API
- **CI/CD**: Github Actions
- **Deployment**: Heroku
- **Testing**: Pytest

## Getting Started

### Prerequisites

Ensure you have the following installed:

- Python 3.8 or higher
- MySQL
- Africa's Talking account credentials
- Google API credentials

### Installation

1. Clone the repository:

```bash
git clone https://github.com/Markkimotho/make-an-order.git
cd make-an-order
   ```

2. Create a Python virtual environment and activate it:

```bash
python -m venv venv
source venv/bin/activate  # On Windows, Use: `venv\Scripts\activate`
```

3. Install required dependencies:

```bash
pip install -r requirements.txt
```

4. Configure environment variables:

 - Create a `.env` file in the project root directory.
 - Add the variables as described in the [Environment Variables](#environment-variables) section.

For test purposes you can use the below `.env` file (make sure to replace the placeholder values with actual values)

```
# .env variables for OAuth2.0 (Google)
GOOGLE_CLIENT_ID=`google_client_id`
GOOGLE_CLIENT_SECRET=`google_client_secret`
APP_SECRET_KEY=`a-very-strong-and-unique-key`

# .env variables for DB
MYSQL_HOST=`localhost`
MYSQL_USER=`root`
MYSQL_PASSWORD=`password`
MYSQL_DB=`customers_orders`

# .env variables for africastalking
AT_USERNAME=`sandbox ` 
AT_API_KEY=`atsk_api-key`
AT_SENDER_ID=`sender_id`
```

5. Initialize the database:

```bash
flask db upgrade
```

6. Run the application

```bash
python app.py
```

#### Mandatory Step if you want to implement OAuth2.0 with Google

1. Set up Google [OAuth2.0](https://support.google.com/cloud/answer/6158849?hl=en)
2. Ensure you have set your Authorized redirect URIs as: `http://127.0.0.1:5001/authorize`

The app will be accessible at `http://127.0.0.1:5001` on your local machine.

7. Now you can test your endpoints using cURL or POSTMAN. You can use the [API Documentation](#api-documentation) as reference material.

## Usage

1. Navigate to the app's homepage.
2. Log in securely using your Google account.
3. Use the web interface or API endpoints to:

- Manage customers.
- Create, update, and track orders.

4. Notifications are sent automatically to customers when their orders are placed.

## API Documentation

### Authentication Endpoints

| Method | Endpoint         | Description                     |
|--------|------------------|---------------------------------|
| GET    | `/login`         | Redirects to Google login page. |
| GET    | `/logout`        | Logs out the authenticated user.|

### Customer Endpoints

| Method | Endpoint                | Description                      |
|--------|-------------------------|----------------------------------|
| POST   | `/customers/register`    Create a new customer.           |
| GET    | `/customers/view_customers`| Retrieve all customers.          |
| GET    | `/customers/view_customers/<id>`   | Retrieve a specific customer.    |
| PUT    | `/customers/update_customers/<id>` | Update customer details.         |
| DELETE | `/customers/delete_customers/<id>` | Delete a customer.               |

Example Request: Create a new Customer

```bash
POST /customers/register
Content-Type: application/json
```

```http
{
  "name": "Mike",
  "phone_number": "+254748995315",
  "code": "CUST001"
}
```

Example Response

```http
{
    "customer_id": 3,
    "message": "Customer registered successfully"
}
```

### Order Endpoints

| Method | Endpoint              | Description                      |
|--------|-----------------------|----------------------------------|
| POST   | `/orders/place_order` | Create a new order.              |
| GET    | `/orders/view_orders`         | Retrieve all orders.          |
| GET    | `/orders/view_orders/<id>`    | Retrieve a specific order.       |
| PUT    | `/orders/update_orders/<id>`    | Update order details.            |
| DELETE | `/orders/delete_orders/<id>`    | Delete an order.                 |

Example Request: Create Order

```bash
POST /orders/place_order
Content-Type: application/json
```

```http
{
  "customer_id": 1,
  "item": "BT Speaker",
  "amount": 100.00
}
```

Example Response

```http
{
    "message": "Order placed successfully!"
}
```

## Testing

The project includes automated tests stored in the `tests/` folder. To run the tests:

```bash
pytest tests/
```

### Test Coverage

- `test_customers.py`: Tests customer-related endpoints.
- `test_orders.py`: Tests order-related endpoints.
- `test_auth.py`: Tests auth-related endpoints.
- `test_models.py`: Tests model-related endpoints.

- Additional tests will be added if need be.

## Environment Variables

| Variable               | Description                                    |
|------------------------|------------------------------------------------|
| `GOOGLE_CLIENT_ID`     | Google OAuth 2.0 Client ID                     |
| `GOOGLE_CLIENT_SECRET` | Google OAuth 2.0 Client Secret                 |
| `APP_SECRET_KEY`       | Secret key for application session management  |
| `MYSQL_HOST`           | Host address for MySQL database                |
| `MYSQL_USER`           | MySQL database username                        |
| `MYSQL_PASSWORD`       | MySQL database password                        |
| `MYSQL_DB`             | MySQL database name                            |
| `AT_USERNAME`          | Africa's Talking API username                  |
| `AT_API_KEY`           | Africa's Talking API key                       |
| `AT_SENDER_ID`         | Africa's Talking sender ID                     |

## Deployment

### CI/CD with GitHub Actions

You can automate deployments to Heroku using GitHub Actions, ensuring that your application is deployed every time changes are pushed to the repository.

#### Steps to Set Up CI/CD with GitHub Actions

1. **Add a GitHub Actions Workflow File**  
   Create a `.github/workflows/deploy.yml` file in your project directory with the following content:

```yaml
name: CI/CD Pipeline for Testing and Deploying

on:
  push:
    branches:
      - main  # Trigger on push to the main branch
  workflow_dispatch: # Allows manual triggering via GitHub UI

jobs:
  test-and-deploy:
    runs-on: ubuntu-latest
    services:
      mysql:
        image: mysql:5.7  # Use MySQL 5.7
        env:
          MYSQL_ROOT_PASSWORD: ${{ secrets.MYSQL_PASSWORD }}
          MYSQL_DATABASE: ${{ secrets.MYSQL_DB }}
        ports:
          - 3306:3306

    steps:
      # Step 1: Checkout code
      - name: Checkout code
        uses: actions/checkout@v3
        with:
          fetch-depth: 0

      # Step 2: Set up Python
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.9'

      # Step 3: Install dependencies
      - name: Install dependencies
        run: |
          python -m venv venv
          source venv/bin/activate
          pip install -r requirements.txt

      # Step 4: Set up environment variables
      - name: Set up environment variables
        run: |
          echo "GOOGLE_CLIENT_ID=${{ secrets.GOOGLE_CLIENT_ID }}" >> .env
          echo "GOOGLE_CLIENT_SECRET=${{ secrets.GOOGLE_CLIENT_SECRET }}" >> .env
          echo "APP_SECRET_KEY=${{ secrets.APP_SECRET_KEY }}" >> .env
          echo "MYSQL_HOST=${{ secrets.MYSQL_HOST }}" >> .env
          echo "MYSQL_PORT=3306" >> .env
          echo "MYSQL_USER=${{ secrets.MYSQL_USER }}" >> .env
          echo "MYSQL_PASSWORD=${{ secrets.MYSQL_PASSWORD }}" >> .env
          echo "MYSQL_DB=${{ secrets.MYSQL_DB }}" >> .env
          echo "AT_USERNAME=${{ secrets.AT_USERNAME }}" >> .env
          echo "AT_API_KEY=${{ secrets.AT_API_KEY }}" >> .env
          echo "AT_SENDER_ID=${{ secrets.AT_SENDER_ID }}" >> .env

      # Step 5: Wait for MySQL to start
      - name: Wait for MySQL to start
        run: |
          for i in {1..10}; do
            if mysqladmin ping -h 127.0.0.1 -P 3306 -u root --password="${{ secrets.MYSQL_PASSWORD }}" --silent; then
              echo "MySQL is up and running."
              break
            fi
            echo "Waiting for MySQL to be available..."
            sleep 5
          done

      # Step 6: Run tests
      - name: Run tests
        env:
          MYSQL_HOST: ${{ secrets.MYSQL_HOST }}
          MYSQL_PORT: 3306
          MYSQL_USER: ${{ secrets.MYSQL_USER }}
          MYSQL_PASSWORD: ${{ secrets.MYSQL_PASSWORD }}
          MYSQL_DB: ${{ secrets.MYSQL_DB }}
        run: |
          source venv/bin/activate
          export PYTHONPATH=$(pwd)
          pytest tests/

      # Step 7: Install Heroku CLI
      - name: Install Heroku CLI
        run: |
          curl https://cli-assets.heroku.com/install.sh | sh
          heroku --version

      # Step 8: Log into Heroku using API key (skip email)
      - name: Login to Heroku using API key
        run: |
          echo "Logging into Heroku..."
          echo "heroku_api_key=${{ secrets.HEROKU_API_KEY }}" > ~/.netrc
          chmod 600 ~/.netrc

      # Step 9: Set up Heroku remote
      - name: Set up Heroku remote
        run: |
          git remote add heroku https://git.heroku.com/make-an-order.git

      # Step 10: Deploy to Heroku
      - name: Deploy to Heroku
        run: |
          git push https://heroku:${{ secrets.HEROKU_API_KEY }}@git.heroku.com/make-an-order.git main
```

2. **Set Up GitHub Secrets**

Go to your GitHub repository settings and add the following secrets under **Settings > Secrets and variables > Actions**:

- HEROKU_API_KEY: Your Heroku API key (found in your Heroku account settings under "Account Settings").
  To obtain the heroku API KEY:

  ```bash
  heroku auth:token
  ```

- Add more secrets:

   ```bash
      GOOGLE_CLIENT_ID
      GOOGLE_CLIENT_SECRET
      APP_SECRET_KEY
      MYSQL_HOST
      MYSQL_PORT
      MYSQL_USER
      MYSQL_PASSWORD
      MYSQL_DB
      AT_USERNAME
      AT_API_KEY
      AT_SENDER_ID
   ```

3. **Trigger Deployment**

Every time you push code to the main branch, this workflow will automatically deploy your application to Heroku.

### Steps to Deploy on Heroku

Assuming you are working on a git initialized directory:

1. **Create a heroku app**:

This will initialize your project on Heroku.

```bash
heroku create <your-app-name>
```

2. **Add JawsDB for MySQL**:

Provision the JawsDB MySQL Heroku add-on:

```bash
heroku addons:create jawsdb:kitefin
```

3. **Add All Changes to Git**:

Stage and commit your changes in Git:

```bash
git add .
git commit -m "Deploy to Heroku"
git push origin main # This push will initiate the Github Actions workflow for CI/CD. 
```

4. **Add Heroku Remote**:

Add Heroku as a remote repository if you haven't already:

```bash
heroku git:remote -a <your-app-name>
```

5. **Push the code to Heroku**:

Deploy the app to Heroku:

```bash
git push heroku main
```

6. **Set environment variables on Heroku**:

Configure environment variables on Heroku:

```bash
heroku config:set GOOGLE_CLIENT_ID=<your_client_id>
heroku config:set GOOGLE_CLIENT_SECRET=<your_client_secret>
heroku config:set MYSQL_USER=<your_mysql_user>
heroku config:set MYSQL_PASSWORD=<your_mysql_password>
heroku config:set MYSQL_HOST=<your_mysql_host>
heroku config:set MYSQL_DB=<your_mysql_database>
heroku config:set APP_SECRET_KEY=<your_app_secret_key>
heroku config:set AT_USERNAME=<your_africas_talking_username>
heroku config:set AT_API_KEY=<your_africas_talking_api_key>
```

## Project Structure

```graphql
.
├── api
│   ├── __init__.py               # Initializes API module
│   ├── customers.py              # Customer-related API endpoints (e.g., create, retrieve, update, delete customers)
│   └── orders.py                 # Order-related API endpoints (e.g., create, retrieve, update, delete orders)
├── .env                          # .env file for environment variables
├── app.py                        # Main application entry point
├── auth
│   ├── __init__.py               # Initializes auth module
│   ├── auth_routes.py            # Routes for authentication (login, logout)
│   └── auth_middleware. py       # Middleware for authentication-related checks
├── config.py                     # Configuration file for environment variables (database, Google OAuth, Africa's Talking)
├── models.py                     # Defines database models (Customer, Order)
├── Procfile                      # Heroku deployment file
├── README.md                     # Project README containing documentation
├── requirements.txt              # List of Python dependencies for the project
├── services
│   ├── __init__.py               # Initializes services module
│   ├── database_service.py       # Service handling database operations (connecting, creating databases)
│   └── sms_service.py            # Service to handle SMS operations using Africa's Talking API
└── tests
    ├── __init__.py               # Initializes the tests module
    ├── test_auth.py              # Unit tests for authentication functionality
    ├── test_customers.py         # Unit tests for customer-related API
    ├── test_models.py            # Unit tests for database models (Customer, Order)
    └── test_orders.py            # Unit tests for order-related API
```
