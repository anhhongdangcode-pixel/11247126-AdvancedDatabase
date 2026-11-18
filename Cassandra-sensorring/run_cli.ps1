# ==============================================================================
# SensorRing Lab - CLI Execution Helper for PowerShell
# ==============================================================================

Write-Host "=== Step 0: Starting cluster & checking status ===" -ForegroundColor Cyan
docker compose up -d
docker compose exec cass1 nodetool status

Write-Host "`n=== Step 1: Checking Tokens & Endpoints ===" -ForegroundColor Cyan
docker compose exec -T cass1 cqlsh -e "SELECT DISTINCT sensor_id, token(sensor_id) FROM iot.readings_by_sensor LIMIT 5;"
docker compose exec cass1 nodetool getendpoints iot readings_by_sensor hn-01

# Create ring1 keyspace with RF=1
docker compose exec -T cass1 cqlsh -e "CREATE KEYSPACE IF NOT EXISTS ring1 WITH replication = {'class': 'SimpleStrategy', 'replication_factor': 1}; CREATE TABLE IF NOT EXISTS ring1.sensors (sensor_id text PRIMARY KEY);"

"hn-01", "hn-02", "hn-03", "hn-04", "hn-05", "hn-06" | ForEach-Object {
    $ep = docker compose exec -T cass1 nodetool getendpoints ring1 sensors $_
    Write-Host "$_ -> $ep"
}
docker compose exec cass1 nodetool status ring1

Write-Host "`n=== Step 2: Query by Partition vs Full Table Scan (Tracing) ===" -ForegroundColor Cyan
Write-Host "1. Query by partition with TRACING:"
docker compose exec -T cass1 cqlsh -e "CONSISTENCY ONE; TRACING ON; SELECT ts, temperature FROM iot.readings_by_sensor WHERE sensor_id = 'hn-01' LIMIT 3;" | Select-String "Executing|live rows|Request complete"

Write-Host "`n2. ALLOW FILTERING with TRACING:"
docker compose exec -T cass1 cqlsh -e "CONSISTENCY ONE; TRACING ON; SELECT count(*) FROM iot.readings_by_sensor WHERE temperature > 31.5 ALLOW FILTERING;" | Select-String "range requests|Executing|live rows|Request complete"

Write-Host "`n=== Step 3: Consistency levels test ===" -ForegroundColor Cyan
python sensorring.py cl

Write-Host "`nDone CLI verification!" -ForegroundColor Green
