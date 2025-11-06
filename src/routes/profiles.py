from fastapi import (
    APIRouter,
    status,
    Depends,
    HTTPException,
)
from security.interfaces import JWTAuthManagerInterface
from security.http import get_token
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError
from database import (
    get_db,
    UserModel,
    UserGroupEnum,
    UserProfileModel
)
from config import get_jwt_auth_manager, get_s3_storage_client
from storages import S3StorageInterface
from exceptions import (
    TokenExpiredError,
    InvalidTokenError,
    S3ConnectionError,
    S3FileUploadError
)

from schemas import ProfileResponseSchema, ProfileCreationSchema


router = APIRouter()


@router.post(
    "/users/{user_id}/profile/",
    response_model=ProfileResponseSchema,
    summary="User's profile creation",
    description="Create a user's profile with full name, gender, date of birth, info and avatar.",
    status_code=status.HTTP_201_CREATED,
)
async def create_user_profile(
    user_id: int,
    user_data: ProfileCreationSchema = Depends(ProfileCreationSchema.as_form),
    jwt_token: str = Depends(get_token),
    db: AsyncSession = Depends(get_db),
    jwt_manager: JWTAuthManagerInterface = Depends(get_jwt_auth_manager),
    s3_client: S3StorageInterface = Depends(get_s3_storage_client)
) -> ProfileResponseSchema:
    try:
        jwt_manager.verify_access_token_or_raise(jwt_token)
    except TokenExpiredError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired."
        )
    except InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token."
        )

    jwt_payload: dict = jwt_manager.decode_access_token(jwt_token)

    try:
        current_user_id = int(jwt_payload.get("user_id"))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload."
        )

    stmt = (
        select(UserModel)
        .options(joinedload(UserModel.group))
        .where(UserModel.id == int(current_user_id))
    )
    current_user = await db.scalar(stmt)

    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found."
        )

    stmt = (
        select(UserModel)
        .options(joinedload(UserModel.profile))
        .where(UserModel.id == int(user_id))
    )
    target_user = await db.scalar(stmt)

    if target_user is None or not target_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or not active."
        )

    if target_user.profile is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User already has a profile."
        )

    if (
        not current_user_id == int(user_id)
        and not current_user.has_group(UserGroupEnum.ADMIN)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have permission to edit this profile."
        )

    user_data.user_id = user_id

    avatar_file = getattr(user_data, "avatar", None)

    if avatar_file:
        contents = await avatar_file.read()

        extension_map = {"JPEG": "jpg", "JPG": "jpg", "PNG": "png"}

        file_extension = avatar_file.content_type.split("/")[-1]
        file_extension = extension_map.get(file_extension.upper(), "jpg")
        file_name = f"avatars/{user_id}_avatar.{file_extension}"
        try:
            await s3_client.upload_file(file_name, contents)
        except (S3ConnectionError, S3FileUploadError) as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to upload avatar. Please try again later."
            )
        
        # avatar_url = await s3_client.get_file_url(file_name)

    try:
        profile_model = UserProfileModel(
            user_id=user_data.user_id,
            first_name=user_data.first_name.lower(),
            last_name=user_data.last_name.lower(),
            gender=user_data.gender,
            date_of_birth=user_data.date_of_birth,
            info=user_data.info,
            avatar=file_name
            # avatar=avatar_url
        )

        db.add(profile_model)
        await db.commit()
        await db.refresh(profile_model)

        return ProfileResponseSchema.model_validate(profile_model)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An error when creating user's profile."
        )
