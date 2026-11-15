from fastapi import HTTPException, status

def bad_request(detail: str):
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)

def unauthorized(detail: str = "Authentication required"):
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)

def forbidden(detail: str = "You do not have permission to perform this action"):
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

def not_found(detail: str = "Resource not found"):
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)

def conflict(detail: str):
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
