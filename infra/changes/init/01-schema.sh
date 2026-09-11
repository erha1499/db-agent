#!/bin/bash
set -euo pipefail
[[ "${CHANGE_PASSWORD}" =~ ^[A-Za-z0-9_-]{24,128}$ ]] || exit 1
[[ "${CHANGE_READER_PASSWORD}" =~ ^[A-Za-z0-9_-]{24,128}$ ]] || exit 1
MYSQL_PWD="${MYSQL_ROOT_PASSWORD}" mysql --protocol=socket -uroot <<SQL
CREATE DATABASE db_agent_changes;
CREATE TABLE db_agent_changes.inventory (
  id BIGINT PRIMARY KEY,
  quantity INT NOT NULL CHECK (quantity BETWEEN 0 AND 1000000),
  version BIGINT NOT NULL CHECK (version BETWEEN 1 AND 2147483646)
) ENGINE=InnoDB;
CREATE TABLE db_agent_changes.change_receipts (
  change_id CHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
  digest CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  item_id BIGINT NOT NULL,
  before_quantity INT NOT NULL,
  after_quantity INT NOT NULL,
  before_version BIGINT NOT NULL,
  after_version BIGINT NOT NULL
) ENGINE=InnoDB;
INSERT INTO db_agent_changes.inventory VALUES (1,10,1),(2,20,1),(3,30,1);
CREATE USER 'db_agent_change_inspector'@'localhost' ACCOUNT LOCK;
GRANT TRIGGER ON db_agent_changes.inventory TO 'db_agent_change_inspector'@'localhost';
GRANT TRIGGER ON db_agent_changes.change_receipts TO 'db_agent_change_inspector'@'localhost';
CREATE DEFINER='db_agent_change_inspector'@'localhost'
FUNCTION db_agent_changes.change_trigger_count() RETURNS BIGINT SQL SECURITY DEFINER READS SQL DATA
RETURN CASE WHEN (SELECT COUNT(*) FROM information_schema.TABLE_PRIVILEGES
WHERE GRANTEE=CONCAT(CHAR(39),'db_agent_change_inspector',CHAR(39),'@',CHAR(39),'localhost',CHAR(39))
AND TABLE_SCHEMA='db_agent_changes' AND TABLE_NAME IN ('inventory','change_receipts')
AND PRIVILEGE_TYPE='TRIGGER') = 2
THEN (SELECT COUNT(*) FROM information_schema.TRIGGERS
WHERE TRIGGER_SCHEMA='db_agent_changes' AND EVENT_OBJECT_TABLE IN ('inventory','change_receipts'))
ELSE -1 END;
GRANT EXECUTE ON FUNCTION db_agent_changes.change_trigger_count TO 'db_agent_change_inspector'@'localhost';
CREATE SQL SECURITY DEFINER VIEW db_agent_changes.change_schema_guard AS
SELECT db_agent_changes.change_trigger_count() AS trigger_count;
CREATE USER 'db_agent_change_reader'@'%' IDENTIFIED BY '${CHANGE_READER_PASSWORD}';
GRANT SELECT ON db_agent_changes.inventory TO 'db_agent_change_reader'@'%';
CREATE USER 'db_agent_changer'@'%' IDENTIFIED BY '${CHANGE_PASSWORD}';
GRANT SELECT, UPDATE(quantity,version) ON db_agent_changes.inventory TO 'db_agent_changer'@'%';
GRANT SELECT, INSERT ON db_agent_changes.change_receipts TO 'db_agent_changer'@'%';
GRANT SELECT, SHOW VIEW ON db_agent_changes.change_schema_guard TO 'db_agent_changer'@'%';
GRANT EXECUTE ON FUNCTION db_agent_changes.change_trigger_count TO 'db_agent_changer'@'%';
GRANT SHOW_ROUTINE ON *.* TO 'db_agent_changer'@'%';
SQL
