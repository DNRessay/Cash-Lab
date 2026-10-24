// Replaced by the deploy workflow with the Lambda Function URL. Localhost is used for local dev.
const PROD_API = "https://REPLACE-ME.lambda-url.eu-west-1.on.aws";
export const API_BASE = ["localhost", "127.0.0.1"].includes(location.hostname) ? "http://localhost:8001" : PROD_API;
