package com.example.shop.client

import retrofit2.Call
import retrofit2.http.*

/** Retrofit client for the Spring item service (base URL ends at the host). */
interface ItemsApi {
    @GET("api/v1/items/{id}")
    fun get(@Path("id") id: Long): Call<Item>

    @POST("api/v1/items")
    fun create(@Body item: Item): Call<Item>
}
