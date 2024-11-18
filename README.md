# make-an-order

## Outline

make-an-order is a RESTful service project for managing customers and orders; which is implemented in Python. The service has authentication and authorization of customers using OpenID connect, order notifications via the **Africa's Talking SMS** gateway, and automated CI/CD deployment.

## Features

1. **Customers and Orders Database**:
   * **Customers**: `name`, `phone_number`, `code`
   * **Orders**: `item`, `amount`, `time`
2. **REST API**:
   * **Customers**: Create and manage customer details.
   * **Orders**: Place orders associated with customers.
3. **Authentication and Authorization**:
   * Uses OAuth2.0 for secure access.
4. **SMS Notifications**:
   * When an order is created, the customer receives an SMS notification via Africa's Talking.
5. **Testing**:
   * Unit tests with coverage checks.
6. **CI/CD**
   * CI/CD pipeline for deployment to a chosen platform (Netflify).

## Tech Stack Used and Why:

* **Backend**: Python
* **Database**: MySQL/SQLite
* **API**: REST
* **Authentication**: OAuth2.0 
* **SMS Gateway**: Africa's Talking
* **Deployment**: PAAS/FAAS/IAAS (e.g., Heroku, Google Cloud Functions)
* **CI/CD:** GitHub Actions (or other preferred CI/CD service TBD)

## How to Install

1. Clone the repository:

```bash
git clone https://github.com/markkimotho/make-an-order.git
cd make-an-order
```

2. Install dependencies:

```bash
//TODO
```

3. Set up environment variables by creating a `.env` file with the below content:

```bash
//TODO
```

## How to Use

1. Start the server:

```bash
//TODO
```

2. Access the API:
   * **Customers** endpoint: /api/customers
   * **Orders** endpoint: /api/orders

## How to Test

1. Run the tests:

```bash
//TODO
```

2. View the coverage details for code quality.

## Deployment
* **CI/CD**: 
* **Deployment to PAAS/FAAS/IAAS**: Automatically deploys upon merging to the main branch.