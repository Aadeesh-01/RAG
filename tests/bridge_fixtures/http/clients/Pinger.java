package com.example.shop.client;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

/** Pings the Go service with OkHttp. */
public class Pinger {
    private final OkHttpClient client = new OkHttpClient();

    public boolean ping() throws java.io.IOException {
        Request request = new Request.Builder().url("http://orders.local/ping").build();
        try (Response response = client.newCall(request).execute()) {
            return response.isSuccessful();
        }
    }
}
