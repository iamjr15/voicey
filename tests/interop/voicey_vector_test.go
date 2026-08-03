package standardwebhooks

import (
	"encoding/json"
	"os"
	"testing"
	"time"
)

type voiceyVector struct {
	Body      string `json:"body"`
	EventID   string `json:"event_id"`
	Secret    string `json:"secret"`
	Signature string `json:"signature"`
	Timestamp int64  `json:"timestamp"`
}

func TestVoiceyInteroperabilityVector(t *testing.T) {
	vectorPath := os.Getenv("VOICEY_VECTOR_PATH")
	raw, err := os.ReadFile(vectorPath)
	if err != nil {
		t.Fatal(err)
	}
	var vector voiceyVector
	if err := json.Unmarshal(raw, &vector); err != nil {
		t.Fatal(err)
	}
	webhook, err := NewWebhook(vector.Secret)
	if err != nil {
		t.Fatal(err)
	}
	signature, err := webhook.Sign(
		vector.EventID,
		time.Unix(vector.Timestamp, 0),
		[]byte(vector.Body),
	)
	if err != nil {
		t.Fatal(err)
	}
	if signature != vector.Signature {
		t.Fatalf("signature mismatch: got %q want %q", signature, vector.Signature)
	}
}
