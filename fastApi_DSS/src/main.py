import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
from typing import Optional, Annotated, Dict

import duckdb
import pytz
from fastapi import FastAPI, HTTPException, Depends
from fastapi import Response
from fastapi.params import Query
from pydantic_settings import BaseSettings, SettingsConfigDict

from orchestrator import DuckDBPostgresETL
from database import TrafficIncidentCreate, TrafficIncident


class AppConfig(BaseSettings):
    DUCKDB_PATH: str = ":memory:"  # Default to in-memory database if not specified
    DEBUG: bool = False  # Add debug configuration

    POSTGRES_HOST: str
    POSTGRES_PORT: int
    POSTGRES_DB: str
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    ETL_RUN_TIME: str
    ETL_TIMEZONE: str

    model_config = SettingsConfigDict(
        env_file="../.env",
        env_file_encoding="utf-8",
        extra="allow"  # Allow extra fields in environment variables
    )

    @property
    def postgres_config(self) -> Dict[str, str]:
        return {
            "host": self.POSTGRES_HOST,
            "port": self.POSTGRES_PORT,
            "database": self.POSTGRES_DB,
            "user": self.POSTGRES_USER,
            "password": self.POSTGRES_PASSWORD,
        }
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session


class DatabaseConnection:
    _instance = None

    def __new__(cls, setting: AppConfig):
        if not cls._instance:
            cls._instance = super(DatabaseConnection, cls).__new__(cls)
            cls._setup_connection(setting)
        return cls._instance

    @classmethod
    def _setup_connection(cls, setting: AppConfig):
        try:
            connection_string = f'postgresql://{setting.POSTGRES_USER}:{setting.POSTGRES_PASSWORD}@{setting.POSTGRES_HOST}:{setting.POSTGRES_PORT}/{setting.POSTGRES_DB}'
            cls.engine = create_engine(connection_string)
            cls.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=cls.engine)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Database connection error: {str(e)}"
            )

    @classmethod
    def get_db(cls) -> Session:
        db = None
        try:
            db = cls.SessionLocal()
            yield db
        finally:
            if db:
                db.close()


class WarehouseConnection:
    _instance: Optional[duckdb.DuckDBPyConnection] = None

    @classmethod
    def get_connection(cls) -> duckdb.DuckDBPyConnection:
        if cls._instance is None:
            raise HTTPException(
                status_code=500,
                detail="Database connection is not initialized."
            )
        return cls._instance

    @classmethod
    def initialize(cls, path: str) -> None:
        if cls._instance is not None:
            cls.close()
        cls._instance = duckdb.connect(path)

    @classmethod
    def close(cls) -> None:
        if cls._instance is not None:
            cls._instance.close()
            cls._instance = None

    @classmethod
    def is_initialized(cls) -> bool:
        return cls._instance is not None


async def get_dw() -> duckdb.DuckDBPyConnection:
    """Dependency for getting database connection"""
    conn = WarehouseConnection.get_connection()
    if not conn:
        raise HTTPException(
            status_code=500,
            detail="Database connection is not available"
        )
    return conn


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('../etl_log.log'),
        logging.StreamHandler()
    ]
)


class ETLManager:
    def __init__(self, settings: AppConfig):
        self.settings = settings
        self.etl_task: Optional[asyncio.Task] = None
        self.logger = logging.getLogger(__name__)
        self.etl = DuckDBPostgresETL(
            settings.DUCKDB_PATH,
            settings.postgres_config,
            self.logger)

        # Parse ETL run time
        run_time = datetime.strptime(settings.ETL_RUN_TIME, "%H:%M").time()
        self.run_time = time(run_time.hour, run_time.minute)
        self.timezone = pytz.timezone(settings.ETL_TIMEZONE)

    async def schedule_next_run(self) -> None:
        """Calculate and wait until the next run time"""
        now = datetime.now(self.timezone)
        target = datetime.combine(now.date(), self.run_time)
        target = self.timezone.localize(target)

        # If today's run time has passed, schedule for tomorrow
        if now >= target:
            target = target + timedelta(days=1)

        # Calculate seconds until next run
        delay = (target - now).total_seconds()
        await asyncio.sleep(delay)

    async def run_etl_loop(self) -> None:
        while True:
            try:
                # self.etl.setup_connection()
                await self.schedule_next_run()
                # Run ETL process
                self.etl.run_daily_etl()
            except Exception as e:
                self.logger.error(f"Error in ETL loop: {str(e)}")
                if self.settings.DEBUG:
                    self.logger.exception("Detailed error information:")

            # Even if there's an error, continue the loop
            await asyncio.sleep(600)  # Wait 10 minutes before checking schedule again

    def start(self) -> None:
        """Start the ETL background task"""
        if self.etl_task is None or self.etl_task.done():
            self.etl_task = asyncio.create_task(self.run_etl_loop())
            self.logger.info("ETL background task started")

    def stop(self) -> None:
        """Stop the ETL background task"""
        if self.etl_task and not self.etl_task.done():
            self.etl_task.cancel()
            self.logger.info("ETL background task stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = AppConfig()
    etl_manager = None
    try:
        # Ensure DuckDB connection is only initialized once
        if not WarehouseConnection.is_initialized():
            WarehouseConnection.initialize(settings.DUCKDB_PATH)
        print(f"Connected to DuckDB at {settings.DUCKDB_PATH}")
        print(f"Debug mode: {settings.DEBUG}")

        # Initialize and start ETL manager
        etl_manager = ETLManager(settings)
        etl_manager.start()

        # Initialize database connection
        DatabaseConnection(settings)

        yield
    except Exception as e:
        print(f"Initialization error: {str(e)}")
        if settings.DEBUG:
            print(f"Configuration: {settings.model_dump()}")
        raise RuntimeError(f"Failed to initialize application: {str(e)}")
    finally:
        if etl_manager:
            etl_manager.stop()
        WarehouseConnection.close()
        print("Closed DuckDB connection")


# FastAPI application
# app = FastAPI(lifespan=lifespan)

app = FastAPI(
        title="AIVerse",
        description="AIVerse Api controller layer",
        version="0.0.1",
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        lifespan=lifespan,
    )

@app.get("/")
async def root():
    return {"message": "Hello World"}

# noinspection SqlDialectInspection
@app.get("/health")
async def health_check(db: duckdb.DuckDBPyConnection = Depends(get_dw)):
    """Health check endpoint that verifies database connection"""
    try:
        # Simple query to verify database connection
        # res = db.execute("SELECT max(accident_id) FROM accident").df()
        # return Response(res.to_json(orient="records"), media_type="application/json")
        db.execute("SELECT 1").df()
        # db.execute("""
        #         DELETE FROM accident WHERE accident_id >= 7728400;
        # """)
        # db.execute("""
        # CREATE OR REPLACE SEQUENCE seq_environment_id START 349;
        # CREATE OR REPLACE SEQUENCE seq_location_id START 624778;
        # CREATE OR REPLACE SEQUENCE seq_weather_id START 13;
        # CREATE OR REPLACE SEQUENCE seq_accident_id START 7728400;
        # CREATE OR REPLACE SEQUENCE seq_twilight_id START 12;
        # CREATE OR REPLACE SEQUENCE seq_wind_id START 12;
        # """)
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Database health check failed: {str(e)}"
        )


# noinspection SqlDialectInspection
@app.get("/chart/1")
async def get_chart_data_1(state: Annotated[Optional[str], Query(alias="state")] = None,
                           city: Annotated[Optional[str], Query(alias="city")] = None,
                           db: duckdb.DuckDBPyConnection = Depends(get_dw)):
    if city and not state:
        raise HTTPException(
            status_code=400,
            detail="State must be provided if City is specified."
        )

    try:
        res = db.execute("""
                SELECT 
                    strftime(date_trunc('month', a.Start_Time), '%Y-%m') as month, a.Severity, COUNT(a.Accident_ID) as count
                FROM accident a
                JOIN location l ON a.Location_ID = l.Location_ID
                WHERE (? IS NULL OR l.State = ?) AND (? IS NULL OR l.City = ?)
                GROUP BY month, a.Severity
                """, [state, state, city, city]).df()
        return Response(res.to_json(orient="records"), media_type="application/json")
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Database error: {str(e)}"
        )

# noinspection SqlNoDataSourceInspection
# noinspection SqlDialectInspection
@app.get("/chart/2")
async def get_chart_data_2(state: Annotated[Optional[str], Query(alias="state")] = None,
                           city: Annotated[Optional[str], Query(alias="city")] = None,
                           db: duckdb.DuckDBPyConnection = Depends(get_dw)):
    if city and not state:
        raise HTTPException(
            status_code=400,
            detail="State must be provided if City is specified."
        )

    try:
        res = db.execute("""
                SELECT strftime(date_trunc('month', a.Start_Time), '%Y-%m') as month, Weather_Condition, Severity, COUNT(a.Accident_ID) as count
                FROM accident a
                JOIN weather w ON a.Weather_Condition_ID = w.Weather_Condition_ID
                JOIN location l ON a.Location_ID = l.Location_ID
                WHERE (? IS NULL OR l.State = ?) AND (? IS NULL OR l.City = ?)
                GROUP BY month, Weather_Condition, Severity
                """, [state, state, city, city]).df()
        return Response(res.to_json(orient="records"), media_type="application/json")
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Database error: {str(e)}"
        )


# noinspection SqlDialectInspection
@app.get("/chart/3")
async def get_chart_data_34(state: Annotated[Optional[str], Query(alias="state")] = None,
                            city: Annotated[Optional[str], Query(alias="city")] = None,
                            db: duckdb.DuckDBPyConnection = Depends(get_dw)):
    if city and not state:
        raise HTTPException(
            status_code=400,
            detail="State must be provided if City is specified."
        )

    try:
        res = db.execute("""
                SELECT 
                    strftime(date_trunc('month', a.Start_Time), '%Y-%m') as month,
                    -- round to 1 decimal place for better performance on the Frontend
                    ROUND(Start_Lat, 2) as grid_lat, 
                    ROUND(Start_Lng, 2) as grid_lng, 
                    COUNT(*) as accident_count
                FROM accident a 
                JOIN location l ON a.Location_ID = l.Location_ID
                WHERE (? IS NULL OR l.State = ?) AND (? IS NULL OR l.City = ?)
                GROUP BY month, grid_lat, grid_lng
                """, [state, state, city, city]).df()
        return Response(res.to_json(orient="records"), media_type="application/json")
    except Exception as e:
        raise HTTPException(
            status_code=00,
            detail=f"Database error: {str(e)}"
        )

# noinspection SqlDialectInspection
@app.get("/chart/4")
async def get_chart_data_34(state: Annotated[Optional[str], Query(alias="state")] = None,
                            city: Annotated[Optional[str], Query(alias="city")] = None,
                            db: duckdb.DuckDBPyConnection = Depends(get_dw)):
    if city and not state:
        raise HTTPException(
            status_code=400,
            detail="State must be provided if City is specified."
        )

    try:
        res = db.execute("""
                SELECT 
                    strftime(date_trunc('month', a.Start_Time), '%Y-%m') as month,
                    ROUND(Start_Lat, 1) as grid_lat, 
                    ROUND(Start_Lng, 1) as grid_lng, 
                    Severity,
                    COUNT(*) as accident_count
                FROM accident a 
                JOIN location l ON a.Location_ID = l.Location_ID
                WHERE (? IS NULL OR l.State = ?) AND (? IS NULL OR l.City = ?)
                GROUP BY month, grid_lat, grid_lng, Severity
                """, [state, state, city, city]).df()
        return Response(res.to_json(orient="records"), media_type="application/json")
    except Exception as e:
        raise HTTPException(
            status_code=00,
            detail=f"Database error: {str(e)}"
        )

# noinspection SqlDialectInspection
@app.get("/chart/5")
async def get_chart_data_5(state: Annotated[Optional[str], Query(alias="state")] = None,
                           city: Annotated[Optional[str], Query(alias="city")] = None,
                           db: duckdb.DuckDBPyConnection = Depends(get_dw)):
    if city and not state:
        raise HTTPException(
            status_code=400,
            detail="State must be provided if City is specified."
        )

    try:
        res = db.execute("""
        SELECT 
            date_part('year', a.Start_Time) AS year, 
            a.Severity,
            COUNT(CASE WHEN e.Amenity THEN 1 END) AS Amenity, 
            COUNT(CASE WHEN e.Bump THEN 1 END) AS Bump, 
            COUNT(CASE WHEN e.Crossing THEN 1 END) AS Crossing, 
            COUNT(CASE WHEN e.Give_Way THEN 1 END) AS Give_Way, 
            COUNT(CASE WHEN e.Junction THEN 1 END) AS Junction, 
            COUNT(CASE WHEN e.No_Exit THEN 1 END) AS No_Exit, 
            COUNT(CASE WHEN e.Railway THEN 1 END) AS Railway, 
            COUNT(CASE WHEN e.Roundabout THEN 1 END) AS Roundabout, 
            COUNT(CASE WHEN e.Station THEN 1 END) AS Station, 
            COUNT(CASE WHEN e.Stop THEN 1 END) AS Stop, 
            COUNT(CASE WHEN e.Traffic_Calming THEN 1 END) AS Traffic_Calming, 
            COUNT(CASE WHEN e.Traffic_Signal THEN 1 END) AS Traffic_Signal, 
            COUNT(CASE WHEN e.Turning_Loop THEN 1 END) AS Turning_Loop
        FROM accident a 
        JOIN environment e ON a.Environment_ID = e.Environment_ID 
        JOIN location l ON a.Location_ID = l.Location_ID
        WHERE (? IS NULL OR l.State = ?) AND (? IS NULL OR l.City = ?)
        GROUP BY year, a.Severity
        ORDER BY year, a.Severity;
        """, [state, state, city, city]).df()
        return Response(res.to_json(orient="records"), media_type="application/json")
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Database error: {str(e)}"
        )

# noinspection SqlDialectInspection
@app.get("/chart/6")
async def get_chart_data_6(state: Annotated[Optional[str], Query(alias="state")] = None,
                           city: Annotated[Optional[str], Query(alias="city")] = None,
                           db: duckdb.DuckDBPyConnection = Depends(get_dw)):
    if city and not state:
        raise HTTPException(
            status_code=400,
            detail="State must be provided if City is specified."
        )
    try:
        res = db.execute("""
                SELECT date_part('hour', a.Start_Time) as hour, date_part('year', a.Start_Time) as year, Severity, COUNT(a.Accident_ID) as count
                FROM accident a JOIN location l ON a.Location_ID = l.Location_ID
                WHERE (? IS NULL OR l.State = ?) AND (? IS NULL OR l.City = ?) 
                GROUP BY hour, year, Severity 
                ORDER BY year, hour, Severity
                """, [state, state, city, city]).df()
        return Response(res.to_json(orient="records"), media_type="application/json")
    except Exception as e:
        raise HTTPException(
            status_code=00,
            detail=f"Database error: {str(e)}"
        )


@app.get("/chart/stats")
async def get_stats(db: duckdb.DuckDBPyConnection = Depends(get_dw)): #CURRENT_DATE or '2016-05-25'
    try:
        total_counts = db.execute("""
            SELECT
                COUNT(*) FILTER (WHERE date_trunc('day', Start_Time) = CAST('2016-05-25' AS DATE)) AS total_accident_today,
                COUNT(*) FILTER (WHERE date_trunc('day', Start_Time) = CAST('2016-05-25' AS DATE) - INTERVAL '1 day') AS total_yesterday
            FROM accident;
        """).df()

        most_accident_city_this_month = db.execute("""
            SELECT l.City, COUNT(*) as count
            FROM accident a     
            JOIN location l ON a.Location_ID = l.Location_ID
            WHERE date_trunc('month', a.Start_Time) = date_trunc('month', CAST('2016-05-25' AS DATE))
            GROUP BY l.City
            ORDER BY count DESC
            LIMIT 1;
        """).df()

        least_accident_city_this_month = db.execute("""
            SELECT l.City, COUNT(*) as count
            FROM accident a
            JOIN location l ON a.Location_ID = l.Location_ID
            WHERE date_trunc('month', a.Start_Time) = date_trunc('month', CAST('2016-05-25' AS DATE))
            GROUP BY l.City
            ORDER BY count ASC
            LIMIT 1;
        """).df()

        count_each_severity_today = db.execute("""
            SELECT Severity, COUNT(*) as count
            FROM accident
            WHERE date_trunc('day', Start_Time) = CAST('2016-05-25' AS DATE)
            GROUP BY Severity;
        """).df()
        print("total_counts:", total_counts)
        print("\n")
        print("most_accident_city_this_month:", most_accident_city_this_month)
        print("\n")
        print("least_city_accidents_this_month:", least_accident_city_this_month)
        print("\n")
        print("count_each_severity_today:", count_each_severity_today)
        print("\n")
        res = {
            "total_accident_today": total_counts.to_dict(orient="records")[0] if not total_counts.empty else {"count": 0},
            "most_accident_city": most_accident_city_this_month.to_dict(orient="records")[0] if not most_accident_city_this_month.empty else {"City": "No Data", "count": 0},
            "least_accident_city": least_accident_city_this_month.to_dict(orient="records")[0] if not least_accident_city_this_month.empty else {"City": "No Data", "count": 0},
            "count_each_severity_today": count_each_severity_today.to_dict(orient="records") if not count_each_severity_today.empty else [],
        }

        return res

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Database error: {str(e)}"
        )

@app.get('/count_each_table')
async def count_table(db: duckdb.DuckDBPyConnection = Depends(get_dw)):
    try:
        accident = db.execute("""SELECT COUNT(*) FROM accident;""").df()
        environment = db.execute("""SELECT COUNT(*) FROM environment;""").df()
        location = db.execute("""SELECT COUNT(*) FROM location;""").df()
        twilight = db.execute("""SELECT COUNT(*) FROM twilight;""").df()
        weather = db.execute("""SELECT COUNT(*) FROM weather;""").df()
        wind = db.execute("""SELECT COUNT(*) FROM wind;""").df()

        res = {
            "accident": accident.to_dict(orient="records")[0],
            "environment": environment.to_dict(orient="records")[0],
            "location": location.to_dict(orient="records")[0],
            "twilight": twilight.to_dict(orient="records")[0],
            "weather": weather.to_dict(orient="records")[0],
            "wind": wind.to_dict(orient="records")[0],
        }

        return res
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Database error: {str(e)}"
        )


@app.post('/accident', response_model=TrafficIncidentCreate)
def create_traffic_accident(accident: TrafficIncidentCreate, db: Session = Depends(DatabaseConnection.get_db)):
    try:
        # Insert accident record
        db_accident = TrafficIncident(**accident.model_dump())

        db.add(db_accident)
        db.commit()
        db.refresh(db_accident)
        return accident
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Database error: {str(e)}"
        )