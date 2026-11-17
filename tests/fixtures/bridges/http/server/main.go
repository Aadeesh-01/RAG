package main

import "net/http"

// ping answers health checks from any client, with any method.
func ping(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/plain")
	w.Write([]byte("pong"))
}

// saveOrder stores an order sent with POST /orders/{id}.
func saveOrder(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	w.WriteHeader(http.StatusCreated)
	w.Write([]byte(id))
}

func main() {
	mux := http.NewServeMux()
	http.HandleFunc("/ping", ping)
	mux.HandleFunc("POST /orders/{id}", saveOrder)
	http.ListenAndServe(":8080", mux)
}
