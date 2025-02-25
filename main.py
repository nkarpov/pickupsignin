from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import create_engine, Column, String, DateTime, MetaData, Table, text
from sqlalchemy.sql import select
from datetime import datetime
from typing import Optional, Dict, Set
from sse_starlette.sse import EventSourceResponse
import asyncio
import json
import select as select_module
from contextlib import asynccontextmanager
import psycopg2
import psycopg2.extensions

# Database setup
DATABASE_URL = "postgresql://postgres:postgres@localhost:5433/postgres"

# Global state
notification_queues = {}
queue_id = 0
db_connection_healthy = True  # Track database connection health

# Background task for handling notifications
async def broadcast_connection_status(status: str, error: str = None):
    message = {
        'type': 'connection_status',
        'data': {'status': status, 'error': error}
    }
    for q in notification_queues.values():
        await q.put(json.dumps(message))

async def handle_pg_notifications(initial_conn):
    global db_connection_healthy
    conn = initial_conn
    retry_count = 0
    max_retries = 5
    base_delay = 1  # Start with 1 second delay
    
    while True:
        try:
            if not conn or conn.closed:
                raise psycopg2.OperationalError("Connection is closed")
                
            if select_module.select([conn], [], [], 1.0)[0]:
                conn.poll()
                while conn.notifies:
                    notify = conn.notifies.pop()
                    print(f"Broadcasting notification to {len(notification_queues)} clients")
                    for q in notification_queues.values():
                        await q.put(notify.payload)
            
            # If we get here, connection is healthy
            if not db_connection_healthy:
                db_connection_healthy = True
                await broadcast_connection_status('connected')
                retry_count = 0  # Reset retry count on successful connection
            
            await asyncio.sleep(0.1)
            
        except (psycopg2.Error, psycopg2.OperationalError) as e:
            db_connection_healthy = False
            error_msg = f"Database connection error: {str(e)}"
            print(error_msg)
            await broadcast_connection_status('disconnected', error_msg)
            
            # Close the old connection if it exists
            if conn and not conn.closed:
                try:
                    conn.close()
                except:
                    pass
            
            # Implement exponential backoff
            if retry_count < max_retries:
                delay = min(30, base_delay * (2 ** retry_count))  # Cap at 30 seconds
                print(f"Retrying connection in {delay} seconds...")
                await asyncio.sleep(delay)
                
                try:
                    # Try to establish a new connection
                    conn = psycopg2.connect(DATABASE_URL)
                    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
                    cur = conn.cursor()
                    cur.execute('LISTEN signin_changes;')
                    retry_count += 1
                    print("Reconnected to database")
                except Exception as e:
                    print(f"Reconnection failed: {e}")
            else:
                print("Max retries reached, waiting for manual intervention")
                await asyncio.sleep(60)  # Wait a minute before trying again
                retry_count = 0  # Reset retry count and try again

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_connection_healthy
    
    try:
        # Create a dedicated connection for LISTEN/NOTIFY
        notify_conn = psycopg2.connect(DATABASE_URL)
        notify_conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        cur = notify_conn.cursor()
        cur.execute('LISTEN signin_changes;')
        db_connection_healthy = True
    except Exception as e:
        db_connection_healthy = False
        print(f"Initial database connection failed: {e}")
        notify_conn = None
    
    # Start background task
    task = asyncio.create_task(handle_pg_notifications(notify_conn))
    
    yield
    
    # Cleanup
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    
    if notify_conn:
        try:
            notify_conn.close()
        except:
            pass

app = FastAPI(lifespan=lifespan)

# Create SQLAlchemy engine
engine = create_engine(DATABASE_URL)
metadata = MetaData()

# Define the signin table
signin_table = Table(
    "signin",
    metadata,
    Column("signin", DateTime, nullable=False),
    Column("signout", DateTime, nullable=True),
    Column("driver", String, nullable=False),
    Column("brand", String, nullable=False),
    Column("orderid", String, nullable=False),
)

# Create tables and triggers
def setup_database():
    # Create the table
    metadata.create_all(engine)
    
    # Create triggers for CDC
    with engine.connect() as conn:
        # Create notification function
        conn.execute(text("""
            CREATE OR REPLACE FUNCTION notify_signin_changes()
            RETURNS trigger AS $$
            DECLARE
                notification json;
            BEGIN
                IF (TG_OP = 'INSERT') THEN
                    notification = json_build_object(
                        'type', 'new_signin',
                        'data', json_build_object(
                            'signin', NEW.signin,
                            'driver', NEW.driver,
                            'brand', NEW.brand,
                            'orderid', NEW.orderid
                        )
                    );
                ELSIF (TG_OP = 'UPDATE') AND (NEW.signout IS NOT NULL AND OLD.signout IS NULL) THEN
                    notification = json_build_object(
                        'type', 'signout',
                        'data', json_build_object(
                            'orderid', NEW.orderid,
                            'signout', NEW.signout
                        )
                    );
                END IF;
                
                IF notification IS NOT NULL THEN
                    PERFORM pg_notify('signin_changes', notification::text);
                END IF;
                
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """))
        
        # Create triggers
        conn.execute(text("""
            DROP TRIGGER IF EXISTS signin_notify_trigger ON signin;
            CREATE TRIGGER signin_notify_trigger
            AFTER INSERT OR UPDATE ON signin
            FOR EACH ROW
            EXECUTE FUNCTION notify_signin_changes();
        """))
        conn.commit()

setup_database()

# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def read_root():
    return FileResponse("static/index.html")

@app.get("/admin")
async def read_admin():
    return FileResponse("static/admin.html")

@app.get("/api/signin")
async def create_signin(driver: str, brand: str, orderid: str):
    with engine.connect() as conn:
        now = datetime.now()
        result = conn.execute(
            signin_table.insert().values(
                signin=now,
                driver=driver,
                brand=brand,
                orderid=orderid
            )
        )
        conn.commit()
    return {"status": "success"}

@app.post("/api/signout/{orderid}")
async def create_signout(orderid: str):
    with engine.connect() as conn:
        now = datetime.now()
        result = conn.execute(
            signin_table.update()
            .where(signin_table.c.orderid == orderid)
            .where(signin_table.c.signout.is_(None))
            .values(signout=now)
        )
        conn.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail="Order not found or already signed out")
    return {"status": "success"}

@app.get("/api/active-signins")
async def get_active_signins():
    return await get_initial_signins()

async def get_initial_signins():
    with engine.connect() as conn:
        result = conn.execute(
            select(signin_table)
            .where(signin_table.c.signout.is_(None))
            .order_by(signin_table.c.signin.desc())
        )
        signins = []
        for row in result:
            signins.append({
                "signin": row.signin.isoformat(),
                "driver": row.driver,
                "brand": row.brand,
                "orderid": row.orderid
            })
        return signins

async def event_generator():
    global queue_id, db_connection_healthy
    # Create a dedicated queue for this connection
    my_queue_id = queue_id
    queue_id += 1
    my_queue = asyncio.Queue()
    notification_queues[my_queue_id] = my_queue
    print(f"New client connected (id: {my_queue_id}). Total clients: {len(notification_queues)}")
    
    try:
        # Check connection health immediately
        if not db_connection_healthy:
            yield {
                "event": "connection_status",
                "data": json.dumps({
                    "status": "disconnected",
                    "error": "Database connection is currently down"
                })
            }
            # Close the connection to trigger a frontend reconnect
            return
            
        # Send initial data
        try:
            initial_data = await get_initial_signins()
            yield {
                "event": "initial_load",
                "data": json.dumps(initial_data)
            }
        except Exception as e:
            print(f"Error getting initial data: {e}")
            yield {
                "event": "connection_status",
                "data": json.dumps({
                    "status": "disconnected",
                    "error": str(e)
                })
            }
            return
        
        # Listen for changes
        while True:
            try:
                # Use a shorter timeout to be more responsive
                payload = await asyncio.wait_for(my_queue.get(), timeout=0.1)
                notification = json.loads(payload)
                print(f"Client {my_queue_id} received notification: {notification['type']}")
                yield {
                    "event": notification['type'],
                    "data": json.dumps(notification['data'])
                }
            except asyncio.TimeoutError:
                # No new notifications, continue waiting
                continue
            except Exception as e:
                print(f"Error processing notification: {e}")
                continue
    except Exception as e:
        print(f"Error in event generator: {e}")
        raise
    finally:
        # Clean up the queue when client disconnects
        del notification_queues[my_queue_id]
        print(f"Client disconnected (id: {my_queue_id}). Remaining clients: {len(notification_queues)}")

@app.get("/api/stream")
async def stream():
    return EventSourceResponse(event_generator())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
