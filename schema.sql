CREATE DATABASE IF NOT EXISTS hematuang_db
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE hematuang_db;

CREATE TABLE IF NOT EXISTS daily_sessions (
    session_date DATE NOT NULL,
    chat_id BIGINT NOT NULL,
    status ENUM('OPEN', 'CLOSED') NOT NULL DEFAULT 'OPEN',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at TIMESTAMP NULL DEFAULT NULL,
    PRIMARY KEY (session_date, chat_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS transactions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    session_date DATE NOT NULL,
    chat_id BIGINT NOT NULL,
    type ENUM('INCOME', 'EXPENSE') NOT NULL,
    amount DECIMAL(15, 2) NOT NULL,
    category VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_session_chat (session_date, chat_id),
    CONSTRAINT fk_transactions_session
        FOREIGN KEY (session_date, chat_id)
        REFERENCES daily_sessions (session_date, chat_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

