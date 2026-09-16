// synthetic fixture
package com.example.inventory;

public class App {
    public static void main(String[] args) {
        String url = System.getenv("INVENTORY_DATABASE_URL");
        System.out.println("inventory-web " + url);
    }
}
