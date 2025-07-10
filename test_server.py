#!/usr/bin/env python3
import socket
import threading
import sys
import json

# --- Globals ---
clients = set()
clients_lock = threading.Lock()
current_score = 0.0
server_running = True


def broadcast_score():
    """Sends the current score to all connected clients."""
    global clients
    with clients_lock:
        # Create a copy of the set to iterate over, as clients might disconnect during the broadcast
        disconnected_clients = set()
        for client_socket in clients:
            try:
                message = {"score": current_score}
                client_socket.sendall((json.dumps(message) + "\n").encode("utf-8"))
            except (socket.error, BrokenPipeError):
                print(f"Client disconnected: {client_socket.getpeername()}")
                disconnected_clients.add(client_socket)

        # Remove clients that were found to be disconnected
        clients -= disconnected_clients


def handle_client(client_socket, client_address):
    """
    Manages a single client connection. Adds the client to the list,
    sends the initial score, and then waits to detect disconnection.
    """
    global clients
    print(f"Accepted connection from {client_address}")
    with clients_lock:
        clients.add(client_socket)

    try:
        # Send the initial score
        initial_message = {"score": current_score}
        client_socket.sendall((json.dumps(initial_message) + "\n").encode("utf-8"))

        # Keep the connection alive and detect when the client disconnects
        while server_running:
            # A simple way to detect disconnection is to try reading from the socket.
            # A disconnected socket will usually return empty bytes or raise an error.
            data = client_socket.recv(1024)
            if not data:
                break  # Client disconnected gracefully
    except socket.error as e:
        print(f"Socket error with {client_address}: {e}")
    finally:
        print(f"Closing connection to {client_address}")
        with clients_lock:
            clients.discard(client_socket)
        client_socket.close()


def start_server(host, port):
    """The main server loop that accepts incoming connections."""
    global server_running
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Allow reusing the address to avoid "Address already in use" errors on quick restarts
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server_socket.bind((host, port))
        server_socket.listen()
        print(f"Server listening on {host}:{port}")

        while server_running:
            try:
                # Set a timeout so the loop can check `server_running` periodically
                server_socket.settimeout(1.0)
                client_socket, client_address = server_socket.accept()
                thread = threading.Thread(
                    target=handle_client, args=(client_socket, client_address)
                )
                thread.daemon = True  # Allows main thread to exit even if client threads are running
                thread.start()
            except socket.timeout:
                continue  # Go back to checking `server_running`
    except Exception as e:
        print(f"Server error: {e}")
    finally:
        print("Server shutting down...")
        server_socket.close()


def main():
    global current_score, server_running

    # --- Argument Parsing ---
    if not (2 <= len(sys.argv) <= 3):
        print(f"Usage: {sys.argv[0]} <port> [initial_score]")
        sys.exit(1)

    try:
        port = int(sys.argv[1])
    except ValueError:
        print(f"Error: Invalid port '{sys.argv[1]}'. Port must be a number.")
        sys.exit(1)

    if len(sys.argv) == 3:
        try:
            current_score = float(sys.argv[2])
        except ValueError:
            print(
                f"Error: Invalid initial score '{sys.argv[2]}'. Score must be a number."
            )
            sys.exit(1)

    # --- Start Server Thread ---
    server_thread = threading.Thread(target=start_server, args=("0.0.0.0", port))
    server_thread.start()

    # --- Interactive CLI Loop ---
    print(f"Initial score set to: {current_score}")
    print("Enter a new score (float) and press Enter. Press CTRL+C to exit.")

    try:
        while True:
            try:
                new_score_str = input(f"Current score is {current_score}. New score: ")
                current_score = float(new_score_str)
                print(f"Score updated to: {current_score}")
                broadcast_score()
            except ValueError:
                print("Invalid input. Please enter a valid floating-point number.")
            except EOFError:  # Happens if stdin is closed
                break
    except KeyboardInterrupt:
        print("\nCTRL+C received.")
    finally:
        server_running = False
        # Wait for the server thread to finish its shutdown sequence
        server_thread.join()
        print("Shutdown complete.")


if __name__ == "__main__":
    main()
