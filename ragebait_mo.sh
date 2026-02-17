curl -X POST https://api.elevenlabs.io/v1/convai/twilio/outbound-call \
     -H "xi-api-key: ${ELEVENLABS_API_KEY:?ELEVENLABS_API_KEY not set}" \
     -H "Content-Type: application/json" \
     -d '{
  "agent_id": "'"${ELEVENLABS_AGENT_ID:?ELEVENLABS_AGENT_ID not set}"'",
  "agent_phone_number_id": "'"${ELEVENLABS_AGENT_PHONE_NUMBER_ID:?ELEVENLABS_AGENT_PHONE_NUMBER_ID not set}"'",
  "to_number": "'"${MO_CELL_NUMBER:?MO_CELL_NUMBER not set}"'",
  "conversation_initiation_client_data": {
    "dynamic_variables": {
      "DYNAMIC_TOPIC": "'"${DYNAMIC_TOPIC:-Iran US 2026 Negotiations}"'"
    }
  }
}'
