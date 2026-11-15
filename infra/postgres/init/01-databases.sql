\getenv chat_password CHAT_DB_PASSWORD
\getenv auth_password AUTH_DB_PASSWORD

CREATE ROLE chat_app LOGIN PASSWORD :'chat_password';
CREATE ROLE auth_app LOGIN PASSWORD :'auth_password';

CREATE DATABASE chat_db OWNER chat_app;
CREATE DATABASE auth_db OWNER auth_app;

REVOKE ALL ON DATABASE chat_db FROM PUBLIC;
REVOKE ALL ON DATABASE auth_db FROM PUBLIC;