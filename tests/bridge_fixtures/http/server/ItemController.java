package com.example.shop;

import org.springframework.web.bind.annotation.*;

/** Items over HTTP; every route is under /api/v1. */
@RestController
@RequestMapping("/api/v1")
public class ItemController {

    @GetMapping("/items/{id}")
    public Item get(@PathVariable long id) {
        Item item = new Item(id, "item " + id);
        return item;
    }

    @PostMapping(value = "/items")
    public Item create(@RequestBody Item item) {
        item.setId(System.nanoTime());
        return item;
    }
}
