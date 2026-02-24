# db_config.py
import mysql.connector

def get_connection():
    return mysql.connector.connect(
        host="localhost",
        user="root",         # your MySQL username
        password="Thavakalthu$2409", # your MySQL password
        database="goat_farm" # database name
    )

